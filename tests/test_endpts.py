"""Tests verifying every endpoint returns the correct response format.

Covers: status codes, response schema fields, L402 402 challenge format,
token non-exposure across all listing endpoints, and recover flow.
"""

import pytest
from unittest.mock import patch, AsyncMock

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Service, Category
from app.utils import generate_edit_token, hash_token


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _create_service(db: AsyncSession, slug: str = "ep-test") -> tuple[Service, str]:
    token = generate_edit_token()
    svc = Service(
        name="Endpoint Test", slug=slug, url="https://ep.example.com",
        description="For endpoint tests", pricing_sats=42,
        pricing_model="per-request", protocol="L402",
        owner_name="Tester", owner_contact="t@test.com",
        edit_token_hash=hash_token(token),
    )
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc, token


# ---------------------------------------------------------------------------
# GET /api/v1/services — list
# ---------------------------------------------------------------------------

class TestListServicesResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"services", "total", "page", "page_size"}
        svc = data["services"][0]
        expected_fields = {
            "id", "name", "slug", "url", "description", "pricing_sats",
            "pricing_model", "protocol", "owner_name", "logo_url",
            "avg_rating", "rating_count", "categories", "created_at",
        }
        assert set(svc.keys()) == expected_fields
        assert "edit_token" not in svc
        assert "edit_token_hash" not in svc
        assert "domain_challenge" not in svc


# ---------------------------------------------------------------------------
# GET /api/v1/services/{slug} — detail
# ---------------------------------------------------------------------------

class TestGetServiceResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/test-api")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test API"
        assert data["slug"] == "test-api"
        assert data["url"] == "https://api.test.com"
        assert isinstance(data["categories"], list)
        assert isinstance(data["created_at"], str)
        assert "edit_token" not in data
        assert "edit_token_hash" not in data

    @pytest.mark.asyncio
    async def test_404_response(self, client: AsyncClient):
        resp = await client.get("/api/v1/services/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Service not found"


# ---------------------------------------------------------------------------
# POST /api/v1/services — create
# ---------------------------------------------------------------------------

class TestCreateServiceResponse:
    @pytest.mark.asyncio
    async def test_response_includes_edit_token(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Create Test",
            "url": "https://create.example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "edit_token" in data
        assert len(data["edit_token"]) == 43
        assert "edit_token_hash" not in data
        assert data["name"] == "Create Test"
        assert data["slug"] == "create-test"

    @pytest.mark.asyncio
    async def test_422_on_missing_fields(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/v1/search
# ---------------------------------------------------------------------------

class TestSearchResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=Test")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"services", "total", "page", "page_size"}
        assert data["total"] >= 1
        svc = data["services"][0]
        assert "edit_token" not in svc
        assert "edit_token_hash" not in svc

    @pytest.mark.asyncio
    async def test_empty_results(self, client: AsyncClient):
        resp = await client.get("/api/v1/search?q=zzzzz_no_match")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


# ---------------------------------------------------------------------------
# GET /api/v1/services/{slug}/ratings
# ---------------------------------------------------------------------------

class TestListRatingsResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 3
        rating = data[0]
        assert set(rating.keys()) == {"id", "score", "comment", "reviewer_name", "created_at"}

    @pytest.mark.asyncio
    async def test_404_for_nonexistent(self, client: AsyncClient):
        resp = await client.get("/api/v1/services/nope/ratings")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/services/{slug}/ratings — create rating
# ---------------------------------------------------------------------------

class TestCreateRatingResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={
            "score": 4, "comment": "Nice", "reviewer_name": "Bot",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert set(data.keys()) == {"id", "score", "comment", "reviewer_name", "created_at"}
        assert data["score"] == 4


# ---------------------------------------------------------------------------
# PATCH /api/v1/services/{slug} — edit
# ---------------------------------------------------------------------------

class TestPatchServiceResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, db: AsyncSession):
        svc, token = await _create_service(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "Patched"},
            headers={"X-Edit-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Patched"
        assert "edit_token" not in data
        assert "edit_token_hash" not in data

    @pytest.mark.asyncio
    async def test_403_format(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "Fail"},
            headers={"X-Edit-Token": "wrong"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Invalid edit token"


# ---------------------------------------------------------------------------
# GET /api/v1/services/bulk — bulk export
# ---------------------------------------------------------------------------

class TestBulkExportResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 1
        svc = data[0]
        assert "edit_token" not in svc
        assert "edit_token_hash" not in svc
        assert "name" in svc
        assert "slug" in svc


# ---------------------------------------------------------------------------
# GET /api/v1/analytics
# ---------------------------------------------------------------------------

class TestAnalyticsResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/analytics")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"total_services", "total_ratings", "avg_price_sats", "top_rated"}
        assert isinstance(data["top_rated"], list)


# ---------------------------------------------------------------------------
# GET /api/v1/services/{slug}/reputation
# ---------------------------------------------------------------------------

class TestReputationResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/reputation")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {
            "service", "slug", "avg_rating", "rating_count",
            "distribution", "recent_reviews",
        }
        assert set(data["distribution"].keys()) == {"1", "2", "3", "4", "5"}
        assert isinstance(data["recent_reviews"], list)


# ---------------------------------------------------------------------------
# POST /api/v1/services/{slug}/recover/generate
# ---------------------------------------------------------------------------

class TestRecoverGenerateResponse:
    @pytest.mark.asyncio
    async def test_response_schema(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="recover-gen")
        resp = await client.post(f"/api/v1/services/{svc.slug}/recover/generate")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"challenge", "verify_url", "expires_in_minutes"}
        assert len(data["challenge"]) == 64  # hex of 32 bytes
        assert data["verify_url"].endswith("/.well-known/satring-verify")
        assert data["expires_in_minutes"] == 30

    @pytest.mark.asyncio
    async def test_404_for_nonexistent(self, client: AsyncClient):
        resp = await client.post("/api/v1/services/nope/recover/generate")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/v1/services/{slug}/recover/verify
# ---------------------------------------------------------------------------

class TestRecoverVerifyResponse:
    @pytest.mark.asyncio
    async def test_no_challenge_returns_400(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="recover-nochall")
        resp = await client.post(f"/api/v1/services/{svc.slug}/recover/verify")
        assert resp.status_code == 400
        assert "challenge" in resp.json()["detail"].lower() or "expired" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_verify_mismatch_returns_403(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="recover-mismatch")
        # Generate a challenge first
        await client.post(f"/api/v1/services/{svc.slug}/recover/generate")

        # Mock the HTTP fetch to return wrong content
        with patch("app.routes.api.httpx.AsyncClient") as MockClient:
            mock_resp = AsyncMock()
            mock_resp.text = "wrong-challenge-value"
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post(f"/api/v1/services/{svc.slug}/recover/verify")
            assert resp.status_code == 403
            assert "does not match" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_verify_success_returns_new_token(self, client: AsyncClient, db: AsyncSession):
        svc, old_token = await _create_service(db, slug="recover-ok")
        # Generate a challenge
        gen_resp = await client.post(f"/api/v1/services/{svc.slug}/recover/generate")
        challenge = gen_resp.json()["challenge"]

        # Mock the HTTP fetch to return the correct challenge
        with patch("app.routes.api.httpx.AsyncClient") as MockClient:
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post(f"/api/v1/services/{svc.slug}/recover/verify")
            assert resp.status_code == 200
            data = resp.json()
            assert "edit_token" in data
            assert len(data["edit_token"]) == 43
            # New token should differ from old
            assert data["edit_token"] != old_token

            # New token should work for editing
            patch_resp = await client.patch(
                f"/api/v1/services/{svc.slug}",
                json={"name": "Recovered"},
                headers={"X-Edit-Token": data["edit_token"]},
            )
            assert patch_resp.status_code == 200
            assert patch_resp.json()["name"] == "Recovered"

            # Old token should no longer work
            patch_resp2 = await client.patch(
                f"/api/v1/services/{svc.slug}",
                json={"name": "Stolen"},
                headers={"X-Edit-Token": old_token},
            )
            assert patch_resp2.status_code == 403

    @pytest.mark.asyncio
    async def test_unreachable_domain_returns_502(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="recover-unreachable")
        await client.post(f"/api/v1/services/{svc.slug}/recover/generate")

        with patch("app.routes.api.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.side_effect = Exception("Connection refused")
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post(f"/api/v1/services/{svc.slug}/recover/verify")
            assert resp.status_code == 502


# ---------------------------------------------------------------------------
# L402 402 challenge format — verify WWW-Authenticate header
# ---------------------------------------------------------------------------

class TestL402ChallengeFormat:
    """When L402 is enforced, 402 responses must include a proper WWW-Authenticate header."""

    @pytest.mark.asyncio
    async def test_bulk_export_402_has_invoice(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "bulkhash",
                "payment_request": "lnbc10000n1bulk",
            }
            resp = await client.get("/api/v1/services/bulk")
            assert resp.status_code == 402
            assert "www-authenticate" in resp.headers
            www_auth = resp.headers["www-authenticate"]
            assert www_auth.startswith("L402 ")
            assert "macaroon=" in www_auth
            assert "invoice=" in www_auth
            assert "lnbc10000n1bulk" in www_auth

    @pytest.mark.asyncio
    async def test_analytics_402_has_invoice(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "analyticshash",
                "payment_request": "lnbc1000n1analytics",
            }
            resp = await client.get("/api/v1/analytics")
            assert resp.status_code == 402
            www_auth = resp.headers["www-authenticate"]
            assert "L402 " in www_auth
            assert "lnbc1000n1analytics" in www_auth

    @pytest.mark.asyncio
    async def test_reputation_402_has_invoice(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "rephash",
                "payment_request": "lnbc1000n1rep",
            }
            resp = await client.get("/api/v1/services/test-api/reputation")
            assert resp.status_code == 402
            www_auth = resp.headers["www-authenticate"]
            assert "L402 " in www_auth
            assert "lnbc1000n1rep" in www_auth

    @pytest.mark.asyncio
    async def test_create_service_402_has_invoice(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "createhash",
                "payment_request": "lnbc100000n1create",
            }
            resp = await client.post("/api/v1/services", json={
                "name": "Gated", "url": "https://gated.example.com",
            })
            assert resp.status_code == 402
            www_auth = resp.headers["www-authenticate"]
            assert "L402 " in www_auth
            assert "lnbc100000n1create" in www_auth

    @pytest.mark.asyncio
    async def test_create_rating_402_has_invoice(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "ratehash",
                "payment_request": "lnbc100n1rate",
            }
            resp = await client.post("/api/v1/services/test-api/ratings", json={
                "score": 4,
            })
            assert resp.status_code == 402
            www_auth = resp.headers["www-authenticate"]
            assert "L402 " in www_auth
            assert "lnbc100n1rate" in www_auth

    @pytest.mark.asyncio
    async def test_402_body_is_json(self, client: AsyncClient):
        """The JSON body should say Payment Required even though the real data is in headers."""
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {
                "payment_hash": "bodyhash",
                "payment_request": "lnbc1000n1body",
            }
            resp = await client.get("/api/v1/services/bulk")
            assert resp.status_code == 402
            assert resp.json()["detail"] == "Payment Required"


# ---------------------------------------------------------------------------
# L402 price amounts — verify correct sats forwarded per endpoint
# ---------------------------------------------------------------------------

class TestL402PriceAmounts:
    """Verify each endpoint passes the correct price to create_invoice."""

    @pytest.mark.asyncio
    async def test_bulk_uses_bulk_price(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {"payment_hash": "h", "payment_request": "lnbc1"}
            await client.get("/api/v1/services/bulk")
            mock_inv.assert_called_once_with(settings.AUTH_BULK_PRICE_SATS, "satring.com bulk export")

    @pytest.mark.asyncio
    async def test_analytics_uses_default_price(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {"payment_hash": "h", "payment_request": "lnbc1"}
            await client.get("/api/v1/analytics")
            mock_inv.assert_called_once_with(settings.AUTH_PRICE_SATS, "satring.com premium API access")

    @pytest.mark.asyncio
    async def test_reputation_uses_default_price(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {"payment_hash": "h", "payment_request": "lnbc1"}
            await client.get("/api/v1/services/test-api/reputation")
            mock_inv.assert_called_once_with(settings.AUTH_PRICE_SATS, "satring.com premium API access")

    @pytest.mark.asyncio
    async def test_create_service_uses_submit_price(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {"payment_hash": "h", "payment_request": "lnbc1"}
            await client.post("/api/v1/services", json={
                "name": "X", "url": "https://x.com",
            })
            mock_inv.assert_called_once_with(settings.AUTH_SUBMIT_PRICE_SATS, "satring.com service submission")

    @pytest.mark.asyncio
    async def test_create_rating_uses_review_price(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"), \
             patch("app.l402.create_invoice", new_callable=AsyncMock) as mock_inv:
            mock_inv.return_value = {"payment_hash": "h", "payment_request": "lnbc1"}
            await client.post("/api/v1/services/test-api/ratings", json={"score": 3})
            mock_inv.assert_called_once_with(settings.AUTH_REVIEW_PRICE_SATS, "satring.com review submission")


# ---------------------------------------------------------------------------
# Free endpoints — must NOT require L402
# ---------------------------------------------------------------------------

class TestFreeEndpoints:
    """Free endpoints should return 200 even when L402 is enforced."""

    @pytest.mark.asyncio
    async def test_list_services_is_free(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.get("/api/v1/services")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_service_is_free(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.get("/api/v1/services/test-api")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_search_is_free(self, client: AsyncClient):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.get("/api/v1/search?q=test")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_list_ratings_is_free(self, client: AsyncClient, sample_service: Service):
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.get("/api/v1/services/test-api/ratings")
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_recover_generate_is_free(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="free-recover")
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.post(f"/api/v1/services/{svc.slug}/recover/generate")
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Token never exposed in any listing endpoint
# ---------------------------------------------------------------------------

class TestTokenNeverExposedAnywhere:
    @pytest.mark.asyncio
    async def test_list_endpoint(self, client: AsyncClient, db: AsyncSession):
        await _create_service(db, slug="hidden-list")
        resp = await client.get("/api/v1/services")
        text = resp.text
        assert "edit_token_hash" not in text
        assert "domain_challenge" not in text

    @pytest.mark.asyncio
    async def test_search_endpoint(self, client: AsyncClient, db: AsyncSession):
        await _create_service(db, slug="hidden-search")
        resp = await client.get("/api/v1/search?q=Endpoint")
        text = resp.text
        assert "edit_token_hash" not in text
        assert "domain_challenge" not in text

    @pytest.mark.asyncio
    async def test_bulk_endpoint(self, client: AsyncClient, db: AsyncSession):
        await _create_service(db, slug="hidden-bulk")
        resp = await client.get("/api/v1/services/bulk")
        text = resp.text
        assert "edit_token_hash" not in text
        assert "domain_challenge" not in text

    @pytest.mark.asyncio
    async def test_detail_endpoint(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, slug="hidden-detail")
        resp = await client.get(f"/api/v1/services/{svc.slug}")
        text = resp.text
        assert "edit_token_hash" not in text
        assert "domain_challenge" not in text

    @pytest.mark.asyncio
    async def test_patch_response(self, client: AsyncClient, db: AsyncSession):
        svc, token = await _create_service(db, slug="hidden-patch")
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "Patched"},
            headers={"X-Edit-Token": token},
        )
        text = resp.text
        assert "edit_token_hash" not in text
        assert "edit_token" not in text  # PATCH shouldn't re-expose the token
        assert "domain_challenge" not in text
