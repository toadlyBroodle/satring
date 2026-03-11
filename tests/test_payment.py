"""Integration tests for app/payment.py: dual-protocol payment gate."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app, limiter
from app.database import get_db

from tests.conftest import _make_db, _teardown_db


@pytest_asyncio.fixture
async def payment_client():
    """Client fixture for payment tests with test-mode enabled."""
    engine, session = await _make_db()

    async def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
    await _teardown_db(engine, session)


class TestPaymentRouting:
    """Test that require_payment routes to correct protocol based on headers."""

    @pytest.mark.anyio
    async def test_test_mode_bypass(self, payment_client):
        """In test mode, all payment gates are bypassed."""
        assert settings.AUTH_ROOT_KEY == "test-mode"
        # Analytics endpoint should work without any auth headers
        resp = await payment_client.get("/api/v1/analytics")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_bulk_export_no_auth_test_mode(self, payment_client):
        """Bulk export works in test mode without auth."""
        resp = await payment_client.get("/api/v1/services/bulk")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.anyio
    async def test_reputation_test_mode(self, payment_client):
        """Reputation endpoint works in test mode (creates service first)."""
        from sqlalchemy import select
        # Create a service directly via API
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Payment Test Service",
            "url": "https://payment-test.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        resp = await payment_client.get(f"/api/v1/services/{slug}/reputation")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_create_rating_test_mode(self, payment_client):
        """Rating creation works in test mode."""
        # Create service first
        resp = await payment_client.post("/api/v1/services", json={
            "name": "Rating Test Service",
            "url": "https://rating-test.example.com",
            "category_ids": [1],
        })
        assert resp.status_code == 201
        slug = resp.json()["slug"]

        resp = await payment_client.post(f"/api/v1/services/{slug}/ratings", json={
            "score": 5,
            "comment": "Great service",
        })
        assert resp.status_code == 201
