"""CORS behavior for /api/ routes (browser-based x402/L402 client support)."""

import pytest
from httpx import AsyncClient


class TestApiCors:
    @pytest.mark.asyncio
    async def test_api_get_has_cors_headers(self, client: AsyncClient):
        """API GET responses are readable cross-origin."""
        resp = await client.get("/api/v1/services")
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"
        expose = resp.headers["access-control-expose-headers"]
        assert "PAYMENT-REQUIRED" in expose
        assert "PAYMENT-RESPONSE" in expose
        assert "WWW-Authenticate" in expose

    @pytest.mark.asyncio
    async def test_options_preflight_returns_204(self, client: AsyncClient):
        """OPTIONS preflight is answered, not 405'd."""
        resp = await client.options(
            "/api/v1/services",
            headers={
                "Origin": "https://agent.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "PAYMENT-SIGNATURE",
            },
        )
        assert resp.status_code == 204
        assert resp.headers["access-control-allow-origin"] == "*"
        assert "PAYMENT-SIGNATURE" in resp.headers["access-control-allow-headers"]

    @pytest.mark.asyncio
    async def test_preflight_advertises_get_only(self, client: AsyncClient):
        """Only GET is offered cross-origin; writes stay blocked by OriginCheck."""
        resp = await client.options("/api/v1/services")
        methods = resp.headers["access-control-allow-methods"]
        assert "GET" in methods
        assert "POST" not in methods

    @pytest.mark.asyncio
    async def test_web_routes_have_no_cors(self, client: AsyncClient):
        """CORS is scoped to /api/; web (HTML) routes are untouched."""
        resp = await client.get("/")
        assert "access-control-allow-origin" not in resp.headers
