import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import USAGE_FLUSH_INTERVAL, USAGE_RETENTION_DAYS
from app.database import async_session
from app.models import RouteUsage, UsageDetail

logger = logging.getLogger("satring.usage")

# In-memory buffer: {(route, method, source, hour_iso): count}
_buffer: dict[tuple[str, str, str, str], int] = defaultdict(int)
# IP sets persist across flushes within the same hour for accurate unique counts
_ip_sets: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)

# Detail buffer: {(dimension, value, hour_iso): count}
_detail_buffer: dict[tuple[str, str, str], int] = defaultdict(int)
_detail_ip_sets: dict[tuple[str, str, str], set[str]] = defaultdict(set)
_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None

EXCLUDED_PREFIXES = ("/static/", "/.well-known/", "/favicon", "/openapi.json", "/docs")

# Max distinct keys allowed in the buffer between flushes (safety cap)
MAX_BUFFER_KEYS = 10_000
# Max unique IPs tracked per (route, method, source, hour) bucket.
# Once reached, hit_count still increments but new IPs are not stored.
MAX_IPS_PER_BUCKET = 50_000

# Patterns to normalize dynamic path segments into placeholders.
# Order matters: longer prefixes first to avoid partial matches.
_NORMALIZE_PATTERNS = [
    # API routes with {slug} sub-resources
    (re.compile(r"^/api/v1/services/[^/]+/(recover/generate|recover/verify|ratings|reputation)"), r"/api/v1/services/{slug}/\1"),
    # API service detail
    (re.compile(r"^/api/v1/services/[^/]+$"), "/api/v1/services/{slug}"),
    # Web routes with {slug} sub-resources
    (re.compile(r"^/services/[^/]+/(edit|delete|recover|rate|reputation-invoice|reputation-result)"), r"/services/{slug}/\1"),
    # Web service detail
    (re.compile(r"^/services/[^/]+$"), "/services/{slug}"),
    # Payment status
    (re.compile(r"^/payment-status/[^/]+$"), "/payment-status/{hash}"),
]


def _normalize_path(path: str) -> str:
    """Replace dynamic path segments with placeholders to bound cardinality."""
    for pattern, replacement in _NORMALIZE_PATTERNS:
        result = pattern.sub(replacement, path)
        if result != path:
            return result
    return path


def record_hit(route: str, method: str, source: str, client_ip: str) -> None:
    """Record a single hit. Called from middleware (non-async, sync-safe)."""
    for prefix in EXCLUDED_PREFIXES:
        if route.startswith(prefix):
            return

    route = _normalize_path(route)

    # Safety: cap buffer size to prevent memory exhaustion from novel paths
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hour_key = hour.isoformat()
    key = (route, method, source, hour_key)
    if key not in _buffer and len(_buffer) >= MAX_BUFFER_KEYS:
        return
    _buffer[key] += 1
    if len(_ip_sets[key]) < MAX_IPS_PER_BUCKET:
        _ip_sets[key].add(client_ip)


# Patterns to extract slug from raw (pre-normalized) paths
_SLUG_PATTERNS = [
    re.compile(r"^/api/v1/services/([^/]+)"),
    re.compile(r"^/services/([^/]+)"),
]


def record_details(path: str, query_params: dict[str, str], client_ip: str) -> None:
    """Record search queries, category filters, and viewed service slugs."""
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hour_key = hour.isoformat()

    details: list[tuple[str, str]] = []

    # Search queries
    q = query_params.get("q", "").strip().lower()
    if q:
        details.append(("query", q[:200]))

    # Category filters
    cat = query_params.get("category", "").strip().lower()
    if cat:
        details.append(("category", cat[:100]))

    # Service slug views
    for pattern in _SLUG_PATTERNS:
        m = pattern.match(path)
        if m:
            slug = m.group(1)
            # Skip non-slug fixed segments
            if slug not in ("bulk",):
                details.append(("slug", slug[:200]))
            break

    for dimension, value in details:
        key = (dimension, value, hour_key)
        if key not in _detail_buffer and len(_detail_buffer) >= MAX_BUFFER_KEYS:
            continue
        _detail_buffer[key] += 1
        if len(_detail_ip_sets[key]) < MAX_IPS_PER_BUCKET:
            _detail_ip_sets[key].add(client_ip)


async def flush() -> None:
    """Snapshot buffer, upsert rows into RouteUsage, and purge old data."""
    async with _lock:
        has_data = bool(_buffer) or bool(_detail_buffer)
        if not has_data:
            return
        snapshot = dict(_buffer)
        _buffer.clear()
        detail_snapshot = dict(_detail_buffer)
        _detail_buffer.clear()
        # Snapshot IP set sizes but keep sets alive for the current hour
        current_hour = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        ).isoformat()
        ip_counts = {k: len(v) for k, v in _ip_sets.items()}
        detail_ip_counts = {k: len(v) for k, v in _detail_ip_sets.items()}
        # Evict IP sets for past hours (no longer needed)
        for sets_dict, hour_idx in [(_ip_sets, 3), (_detail_ip_sets, 2)]:
            stale = [k for k in sets_dict if k[hour_idx] != current_hour]
            for k in stale:
                del sets_dict[k]

    async with async_session() as db:
        for (route, method, source, hour_key), count in snapshot.items():
            hour = datetime.fromisoformat(hour_key)
            unique = ip_counts.get((route, method, source, hour_key), 0)
            result = await db.execute(
                select(RouteUsage).where(
                    RouteUsage.route == route,
                    RouteUsage.method == method,
                    RouteUsage.source == source,
                    RouteUsage.hour == hour,
                )
            )
            row = result.scalars().first()
            if row:
                row.hit_count += count
                # For the current hour the IP set is still accumulating,
                # so overwrite with the latest total. For past hours the
                # final count was captured before eviction.
                row.unique_ips = unique
            else:
                db.add(RouteUsage(
                    route=route, method=method, source=source,
                    hour=hour, hit_count=count, unique_ips=unique,
                ))

        # Flush detail buffer
        for (dimension, value, hour_key), count in detail_snapshot.items():
            hour = datetime.fromisoformat(hour_key)
            unique = detail_ip_counts.get((dimension, value, hour_key), 0)
            result = await db.execute(
                select(UsageDetail).where(
                    UsageDetail.dimension == dimension,
                    UsageDetail.value == value,
                    UsageDetail.hour == hour,
                )
            )
            row = result.scalars().first()
            if row:
                row.hit_count += count
                row.unique_ips = unique
            else:
                db.add(UsageDetail(
                    dimension=dimension, value=value,
                    hour=hour, hit_count=count, unique_ips=unique,
                ))

        # Purge old data (bulk DELETE, no loading into Python)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=USAGE_RETENTION_DAYS)
        await db.execute(
            delete(RouteUsage).where(RouteUsage.hour < cutoff)
        )
        await db.execute(
            delete(UsageDetail).where(UsageDetail.hour < cutoff)
        )

        await db.commit()


async def _flush_loop() -> None:
    """Periodically flush the buffer to the database."""
    try:
        while True:
            await asyncio.sleep(USAGE_FLUSH_INTERVAL)
            try:
                await flush()
            except Exception:
                logger.exception("Usage flush failed")
    except asyncio.CancelledError:
        pass


def start_flush_task() -> None:
    global _flush_task
    _flush_task = asyncio.create_task(_flush_loop())


async def stop_flush_task() -> None:
    global _flush_task
    if _flush_task:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
    # Final flush on shutdown
    await flush()
