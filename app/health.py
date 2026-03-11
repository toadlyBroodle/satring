"""Background service health monitoring.

Probes registered services for liveness and updates their status.
Detects L402 and x402 protocols via response headers.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.models import Service
from app.utils import extract_domain, is_public_hostname

logger = logging.getLogger("satring.health")

_probe_task: asyncio.Task | None = None


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

            # Check for L402 paywall
            www_auth = resp.headers.get("www-authenticate", "")
            if resp.status_code == 402 and ("L402" in www_auth or "LSAT" in www_auth):
                metadata["detected_protocol"] = "L402"
                return "live", metadata

            # Check for x402 paywall
            payment_required = resp.headers.get("payment-required", "")
            if resp.status_code == 402 and payment_required:
                metadata["detected_protocol"] = "x402"
                return "live", metadata

            # Generic 402 without recognized headers
            if resp.status_code == 402:
                metadata["detected_protocol"] = "unknown_402"
                return "live", metadata

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

        tasks = [_probe_one(svc) for svc in services]
        await asyncio.gather(*tasks, return_exceptions=True)
        await db.commit()

    logger.info(f"Health probe complete: {len(services)} services checked")


async def _health_loop():
    """Background task: run probe_all on an interval."""
    try:
        while True:
            await asyncio.sleep(settings.HEALTH_PROBE_INTERVAL)
            try:
                await probe_all()
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
