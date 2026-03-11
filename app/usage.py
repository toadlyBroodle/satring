import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import USAGE_FLUSH_INTERVAL, USAGE_RETENTION_DAYS
from app.database import async_session
from app.models import EndpointUsage

logger = logging.getLogger("satring.usage")

# In-memory buffer: {(endpoint, method, source, hour_iso): count}
_buffer: dict[tuple[str, str, str, str], int] = defaultdict(int)
# IP sets persist across flushes within the same hour for accurate unique counts
_ip_sets: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
_lock = asyncio.Lock()
_flush_task: asyncio.Task | None = None

EXCLUDED_PREFIXES = ("/static/", "/.well-known/", "/favicon", "/openapi.json", "/docs")

# Max distinct keys allowed in the buffer between flushes (safety cap)
MAX_BUFFER_KEYS = 10_000
# Max unique IPs tracked per (endpoint, method, source, hour) bucket.
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


def record_hit(endpoint: str, method: str, source: str, client_ip: str) -> None:
    """Record a single hit. Called from middleware (non-async, sync-safe)."""
    for prefix in EXCLUDED_PREFIXES:
        if endpoint.startswith(prefix):
            return

    endpoint = _normalize_path(endpoint)

    # Safety: cap buffer size to prevent memory exhaustion from novel paths
    hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0, tzinfo=None)
    hour_key = hour.isoformat()
    key = (endpoint, method, source, hour_key)
    if key not in _buffer and len(_buffer) >= MAX_BUFFER_KEYS:
        return
    _buffer[key] += 1
    if len(_ip_sets[key]) < MAX_IPS_PER_BUCKET:
        _ip_sets[key].add(client_ip)


async def flush() -> None:
    """Snapshot buffer, upsert rows into EndpointUsage, and purge old data."""
    async with _lock:
        if not _buffer:
            return
        snapshot = dict(_buffer)
        _buffer.clear()
        # Snapshot IP set sizes but keep sets alive for the current hour
        current_hour = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0, tzinfo=None
        ).isoformat()
        ip_counts = {k: len(v) for k, v in _ip_sets.items()}
        # Evict IP sets for past hours (no longer needed)
        stale = [k for k in _ip_sets if k[3] != current_hour]
        for k in stale:
            del _ip_sets[k]

    async with async_session() as db:
        for (endpoint, method, source, hour_key), count in snapshot.items():
            hour = datetime.fromisoformat(hour_key)
            unique = ip_counts.get((endpoint, method, source, hour_key), 0)
            result = await db.execute(
                select(EndpointUsage).where(
                    EndpointUsage.endpoint == endpoint,
                    EndpointUsage.method == method,
                    EndpointUsage.source == source,
                    EndpointUsage.hour == hour,
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
                db.add(EndpointUsage(
                    endpoint=endpoint, method=method, source=source,
                    hour=hour, hit_count=count, unique_ips=unique,
                ))

        # Purge old data (bulk DELETE, no loading into Python)
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=USAGE_RETENTION_DAYS)
        await db.execute(
            delete(EndpointUsage).where(EndpointUsage.hour < cutoff)
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
