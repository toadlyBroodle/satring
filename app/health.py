"""Background service health monitoring.

Probes registered services for liveness and updates their status.
Detects L402 and x402 protocols via response headers.
Records probe history for uptime/latency tracking.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import delete, func, select

from app.config import settings
from app.database import async_session
from app.models import ProbeHistory, Service
from app.utils import extract_domain, is_public_hostname

logger = logging.getLogger("satring.health")

_probe_task: asyncio.Task | None = None

# Rolling window for uptime/latency calculations
_ROLLING_WINDOW_DAYS = 7
# Keep probe history for this many days before cleanup
_HISTORY_RETENTION_DAYS = 30


async def probe_service(service: Service, timeout: int) -> tuple[str, dict]:
    """Probe a single service URL and return (status, metadata).

    Returns:
        ("live", {...})       if a valid 402 paywall is detected
        ("confirmed", {...})  if reachable but no 402
        ("dead", {...})       if unreachable/timeout
    """
    hostname = extract_domain(service.url)
    if not hostname or not is_public_hostname(hostname):
        return service.status, {"skipped": True, "reason": "private/unresolvable hostname"}

    metadata = {}
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            resp = await client.get(service.url)
            metadata["status_code"] = resp.status_code
            metadata["response_time_ms"] = resp.elapsed.total_seconds() * 1000 if resp.elapsed else None

            # Check for L402 and x402 paywalls
            www_auth = resp.headers.get("www-authenticate", "")
            has_l402 = resp.status_code == 402 and ("L402" in www_auth or "LSAT" in www_auth)
            payment_required = resp.headers.get("payment-required", "")
            has_x402 = resp.status_code == 402 and bool(payment_required)

            if has_l402 and has_x402:
                metadata["detected_protocol"] = "L402+x402"
                return "live", metadata
            if has_l402:
                metadata["detected_protocol"] = "L402"
                return "live", metadata
            if has_x402:
                metadata["detected_protocol"] = "x402"
                return "live", metadata

            # Generic 402 without recognized headers
            if resp.status_code == 402:
                metadata["detected_protocol"] = "unknown_402"
                return "live", metadata

            # Redirects mean the registered URL is stale
            if 300 <= resp.status_code < 400:
                metadata["detected_protocol"] = "none"
                return "dead", metadata

            # Reachable but no paywall
            metadata["detected_protocol"] = "none"
            return "confirmed", metadata

    except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as exc:
        metadata["error"] = str(exc)[:200]
        return "dead", metadata


async def probe_all():
    """Probe all non-purged services and update their status."""
    semaphore = asyncio.Semaphore(settings.HEALTH_PROBE_CONCURRENCY)
    timeout = settings.HEALTH_PROBE_TIMEOUT
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async with async_session() as db:
        result = await db.execute(
            select(Service).where(Service.status != "purged")
        )
        services = result.scalars().all()

        async def _probe_one(svc: Service):
            async with semaphore:
                new_status, metadata = await probe_service(svc, timeout)

                if metadata.get("skipped"):
                    return

                svc.last_probed_at = now

                if new_status == "dead" and svc.status != "dead":
                    svc.dead_since = now
                elif new_status != "dead":
                    svc.dead_since = None

                svc.status = new_status

                # Record probe history
                history = ProbeHistory(
                    service_id=svc.id,
                    probed_at=now,
                    status=new_status,
                    response_time_ms=metadata.get("response_time_ms"),
                    detected_protocol=metadata.get("detected_protocol"),
                    status_code=metadata.get("status_code"),
                    error=metadata.get("error", "")[:200] if metadata.get("error") else None,
                )
                db.add(history)

                # Update rolling stats
                svc.total_checks = (svc.total_checks or 0) + 1
                if new_status != "dead":
                    svc.successful_checks = (svc.successful_checks or 0) + 1

                # Compute rolling 7-day avg latency
                cutoff = now - timedelta(days=_ROLLING_WINDOW_DAYS)
                latency_result = await db.execute(
                    select(func.avg(ProbeHistory.response_time_ms))
                    .where(ProbeHistory.service_id == svc.id)
                    .where(ProbeHistory.probed_at >= cutoff)
                    .where(ProbeHistory.response_time_ms.is_not(None))
                )
                avg_latency = latency_result.scalar()
                svc.avg_latency_ms = round(avg_latency, 1) if avg_latency is not None else None

        tasks = [_probe_one(svc) for svc in services]
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.commit()

    logger.info(f"Health probe complete: {len(services)} services checked")


async def _cleanup_old_history(db) -> int:
    """Delete probe_history rows older than the retention period. Returns count deleted."""
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=_HISTORY_RETENTION_DAYS)
    result = await db.execute(
        delete(ProbeHistory).where(ProbeHistory.probed_at < cutoff)
    )
    return result.rowcount


async def _health_loop():
    """Background task: run probe_all on an interval."""
    try:
        while True:
            await asyncio.sleep(settings.HEALTH_PROBE_INTERVAL)
            try:
                await probe_all()
                # Cleanup old history once per cycle
                async with async_session() as db:
                    deleted = await _cleanup_old_history(db)
                    await db.commit()
                    if deleted:
                        logger.info(f"Cleaned up {deleted} old probe_history rows")
            except Exception:
                logger.exception("Health probe failed")
    except asyncio.CancelledError:
        pass


def start_health_task():
    global _probe_task
    _probe_task = asyncio.create_task(_health_loop())


async def stop_health_task():
    global _probe_task
    if _probe_task:
        _probe_task.cancel()
        try:
            await _probe_task
        except asyncio.CancelledError:
            pass
        _probe_task = None
