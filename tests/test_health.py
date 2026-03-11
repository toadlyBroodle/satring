"""Tests for app/health.py: service health monitoring."""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.health import probe_service
from app.models import Service


def _make_service(url="https://api.example.com", status="unverified"):
    svc = MagicMock(spec=Service)
    svc.url = url
    svc.status = status
    return svc


class TestProbeService:
    @pytest.mark.anyio
    async def test_l402_402_returns_live(self):
        """A 402 with WWW-Authenticate L402 header should be detected as live."""
        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.headers = {"www-authenticate": 'L402 macaroon="abc", invoice="lnbc..."'}
        mock_resp.elapsed = MagicMock()
        mock_resp.elapsed.total_seconds.return_value = 0.5

        with patch("app.health.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with patch("app.health.is_public_hostname", return_value=True):
                status, meta = await probe_service(_make_service(), timeout=10)

        assert status == "live"
        assert meta["detected_protocol"] == "L402"

    @pytest.mark.anyio
    async def test_x402_402_returns_live(self):
        """A 402 with PAYMENT-REQUIRED header should be detected as live."""
        mock_resp = MagicMock()
        mock_resp.status_code = 402
        mock_resp.headers = {"www-authenticate": "", "payment-required": "eyJ4NDAyVmVyc2lvbiI6Mn0="}
        mock_resp.elapsed = MagicMock()
        mock_resp.elapsed.total_seconds.return_value = 0.3

        with patch("app.health.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with patch("app.health.is_public_hostname", return_value=True):
                status, meta = await probe_service(_make_service(), timeout=10)

        assert status == "live"
        assert meta["detected_protocol"] == "x402"

    @pytest.mark.anyio
    async def test_200_returns_confirmed(self):
        """A 200 response means reachable but no paywall."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        mock_resp.elapsed = MagicMock()
        mock_resp.elapsed.total_seconds.return_value = 0.1

        with patch("app.health.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.return_value = mock_resp
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with patch("app.health.is_public_hostname", return_value=True):
                status, meta = await probe_service(_make_service(), timeout=10)

        assert status == "confirmed"
        assert meta["detected_protocol"] == "none"

    @pytest.mark.anyio
    async def test_timeout_returns_dead(self):
        """A timeout should mark the service as dead."""
        import httpx

        with patch("app.health.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get.side_effect = httpx.TimeoutException("timed out")
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = mock_client

            with patch("app.health.is_public_hostname", return_value=True):
                status, meta = await probe_service(_make_service(), timeout=10)

        assert status == "dead"
        assert "error" in meta

    @pytest.mark.anyio
    async def test_private_ip_skipped(self):
        """Services on private IPs should be skipped (SSRF protection)."""
        with patch("app.health.is_public_hostname", return_value=False):
            svc = _make_service(url="https://192.168.1.1/api")
            status, meta = await probe_service(svc, timeout=10)

        assert meta.get("skipped") is True
