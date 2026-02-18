"""Tests for edit token utilities, web edit flow, API PATCH flow, and delete flow."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Rating
from app.utils import generate_edit_token, hash_token, verify_edit_token


# ---------------------------------------------------------------------------
# Unit: token utilities
# ---------------------------------------------------------------------------

class TestTokenUtils:
    def test_generate_edit_token_length(self):
        token = generate_edit_token()
        assert len(token) == 43  # 32 bytes -> 43 URL-safe base64 chars

    def test_generate_edit_token_unique(self):
        tokens = {generate_edit_token() for _ in range(100)}
        assert len(tokens) == 100

    def test_hash_token_deterministic(self):
        token = "test-token-123"
        assert hash_token(token) == hash_token(token)

    def test_hash_token_is_hex_sha256(self):
        h = hash_token("hello")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_verify_edit_token_correct(self):
        token = generate_edit_token()
        h = hash_token(token)
        assert verify_edit_token(token, h) is True

    def test_verify_edit_token_wrong(self):
        token = generate_edit_token()
        h = hash_token(token)
        assert verify_edit_token("wrong-token", h) is False


# ---------------------------------------------------------------------------
# Helper: create a service with an edit token
# ---------------------------------------------------------------------------

async def create_service_with_token(db: AsyncSession) -> tuple[Service, str]:
    """Create a test service and return (service, plaintext_token)."""
    token = generate_edit_token()
    svc = Service(
        name="Editable API",
        slug="editable-api",
        url="https://editable.example.com",
        description="Original description",
        pricing_sats=100,
        pricing_model="per-request",
        protocol="L402",
        owner_name="Owner",
        owner_contact="owner@example.com",
        edit_token_hash=hash_token(token),
    )
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc, token


# ---------------------------------------------------------------------------
# Integration: Web edit flow
# ---------------------------------------------------------------------------

class TestWebEditFlow:
    @pytest.mark.asyncio
    async def test_edit_page_without_token_shows_input(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.get(f"/services/{svc.slug}/edit")
        assert resp.status_code == 200
        assert "Enter your edit token" in resp.text

    @pytest.mark.asyncio
    async def test_edit_page_with_valid_token_shows_form(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.get(f"/services/{svc.slug}/edit?token={token}")
        assert resp.status_code == 200
        assert "SAVE CHANGES" in resp.text
        assert svc.name in resp.text
        # URL should be shown read-only
        assert svc.url in resp.text

    @pytest.mark.asyncio
    async def test_edit_page_with_invalid_token_shows_input(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.get(f"/services/{svc.slug}/edit?token=bad-token")
        assert resp.status_code == 200
        assert "Enter your edit token" in resp.text

    @pytest.mark.asyncio
    async def test_edit_post_with_valid_token_updates(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.post(
            f"/services/{svc.slug}/edit",
            content=f"edit_token={token}&name=Updated+Name&description=Updated+desc&protocol=X402&pricing_sats=200&pricing_model=flat&owner_name=New+Owner&owner_contact=new%40example.com&logo_url=&categories=9",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert f"/services/{svc.slug}" in resp.headers["location"]

        await db.refresh(svc)
        assert svc.name == "Updated Name"
        assert svc.description == "Updated desc"
        assert svc.protocol == "X402"
        assert svc.pricing_sats == 200

    @pytest.mark.asyncio
    async def test_edit_post_with_invalid_token_returns_403(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.post(f"/services/{svc.slug}/edit", data={
            "edit_token": "wrong-token",
            "name": "Hacked",
        }, follow_redirects=False)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_edit_post_with_missing_token_returns_403(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.post(f"/services/{svc.slug}/edit", data={
            "name": "Hacked",
        }, follow_redirects=False)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_edit_nonexistent_service_returns_404(self, client: AsyncClient):
        resp = await client.get("/services/no-such/edit")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Integration: API PATCH flow
# ---------------------------------------------------------------------------

class TestAPIPatchFlow:
    @pytest.mark.asyncio
    async def test_patch_with_valid_token_updates(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "API Updated", "pricing_sats": 999},
            headers={"X-Edit-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "API Updated"
        assert data["pricing_sats"] == 999
        # URL should remain unchanged
        assert data["url"] == "https://editable.example.com"

    @pytest.mark.asyncio
    async def test_patch_with_invalid_token_returns_403(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "Hacked"},
            headers={"X-Edit-Token": "bad-token"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_patch_without_token_returns_422(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await create_service_with_token(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"name": "Hacked"},
        )
        assert resp.status_code == 422  # missing required header

    @pytest.mark.asyncio
    async def test_patch_nonexistent_returns_404(self, client: AsyncClient, db: AsyncSession):
        resp = await client.patch(
            "/api/v1/services/no-such",
            json={"name": "Hacked"},
            headers={"X-Edit-Token": "any"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_patch_partial_update(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.patch(
            f"/api/v1/services/{svc.slug}",
            json={"description": "Only desc changed"},
            headers={"X-Edit-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "Only desc changed"
        assert data["name"] == "Editable API"  # unchanged


# ---------------------------------------------------------------------------
# Security: GET never exposes token
# ---------------------------------------------------------------------------

class TestTokenNotExposed:
    @pytest.mark.asyncio
    async def test_detail_page_does_not_expose_token_hash(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.get(f"/services/{svc.slug}")
        assert svc.edit_token_hash not in resp.text
        assert token not in resp.text

    @pytest.mark.asyncio
    async def test_api_get_does_not_expose_token_hash(self, client: AsyncClient, db: AsyncSession):
        svc, token = await create_service_with_token(db)
        resp = await client.get(f"/api/v1/services/{svc.slug}")
        data = resp.json()
        assert "edit_token_hash" not in data
        assert "edit_token" not in data


# ---------------------------------------------------------------------------
# Integration: API create returns token
# ---------------------------------------------------------------------------

class TestAPICreateReturnsToken:
    @pytest.mark.asyncio
    async def test_create_service_returns_edit_token(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/api/v1/services", json={
            "name": "Token Test API",
            "url": "https://tokentest.example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "edit_token" in data
        assert len(data["edit_token"]) == 43

        # Verify token works
        patch_resp = await client.patch(
            f"/api/v1/services/{data['slug']}",
            json={"name": "Renamed"},
            headers={"X-Edit-Token": data["edit_token"]},
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["name"] == "Renamed"


# ---------------------------------------------------------------------------
# Integration: Web submit returns token on success page
# ---------------------------------------------------------------------------

class TestWebSubmitReturnsToken:
    @pytest.mark.asyncio
    async def test_submit_shows_edit_token(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", content="name=Token+Display+Test&url=https%3A%2F%2Ftokendisplay.example.com&categories=9", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200
        assert "Token Display Test" in resp.text
        # The token should be displayed somewhere on the page
        assert "edit-token" in resp.text


# ---------------------------------------------------------------------------
# Integration: Delete lifecycle (class-scoped DB — earlier tests create
# services, later tests delete them)
# ---------------------------------------------------------------------------

import re
from sqlalchemy import func


class TestDeleteLifecycle:
    """Shares one DB across all methods so services accumulate then get deleted."""

    # -- setup: create services via web + API, add ratings ----------------

    @pytest.mark.asyncio
    async def test_1_web_submit_creates_service(self, class_client: AsyncClient, class_db: AsyncSession):
        resp = await class_client.post("/submit", content="name=Web+Created&url=https%3A%2F%2Fweb-created.example.com&description=From+web+form&categories=9", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200
        match = re.search(r'id="edit-token"[^>]*>([^<]+)<', resp.text)
        assert match
        # stash on the class so later tests can use it
        TestDeleteLifecycle.web_token = match.group(1).strip()

        svc = (await class_db.execute(
            select(Service).where(Service.name == "Web Created")
        )).scalars().first()
        assert svc is not None
        TestDeleteLifecycle.web_slug = svc.slug

    @pytest.mark.asyncio
    async def test_2_api_create_service(self, class_client: AsyncClient, class_db: AsyncSession):
        resp = await class_client.post("/api/v1/services", json={
            "name": "API Created",
            "url": "https://api-created.example.com",
            "category_ids": [9],
        })
        assert resp.status_code == 201
        data = resp.json()
        TestDeleteLifecycle.api_slug = data["slug"]
        TestDeleteLifecycle.api_token = data["edit_token"]
        TestDeleteLifecycle.api_service_id = data["id"]

    @pytest.mark.asyncio
    async def test_3_add_ratings_to_api_service(self, class_client: AsyncClient, class_db: AsyncSession):
        slug = TestDeleteLifecycle.api_slug
        for score in (5, 4, 3):
            resp = await class_client.post(f"/api/v1/services/{slug}/ratings", json={
                "score": score, "comment": f"Score {score}", "reviewer_name": "Tester",
            })
            assert resp.status_code == 201

        count = (await class_db.execute(
            select(func.count(Rating.id)).where(Rating.service_id == TestDeleteLifecycle.api_service_id)
        )).scalar()
        assert count == 3

    # -- auth checks (services survive) -----------------------------------

    @pytest.mark.asyncio
    async def test_4_web_delete_invalid_token_returns_403(self, class_client: AsyncClient, class_db: AsyncSession):
        resp = await class_client.post(f"/services/{TestDeleteLifecycle.web_slug}/delete", data={
            "edit_token": "wrong-token",
        }, follow_redirects=False)
        assert resp.status_code == 403

        svc = (await class_db.execute(
            select(Service).where(Service.slug == TestDeleteLifecycle.web_slug)
        )).scalars().first()
        assert svc is not None

    @pytest.mark.asyncio
    async def test_5_api_delete_invalid_token_returns_403(self, class_client: AsyncClient, class_db: AsyncSession):
        resp = await class_client.delete(
            f"/api/v1/services/{TestDeleteLifecycle.api_slug}",
            headers={"X-Edit-Token": "bad-token"},
        )
        assert resp.status_code == 403

        svc = (await class_db.execute(
            select(Service).where(Service.slug == TestDeleteLifecycle.api_slug)
        )).scalars().first()
        assert svc is not None

    # -- actual deletes (services + cascaded ratings removed) -------------

    @pytest.mark.asyncio
    async def test_6_web_delete_removes_service(self, class_client: AsyncClient, class_db: AsyncSession):
        resp = await class_client.post(f"/services/{TestDeleteLifecycle.web_slug}/delete", data={
            "edit_token": TestDeleteLifecycle.web_token,
        }, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        svc = (await class_db.execute(
            select(Service).where(Service.slug == TestDeleteLifecycle.web_slug)
        )).scalars().first()
        assert svc is None

    @pytest.mark.asyncio
    async def test_7_api_delete_cascades_ratings(self, class_client: AsyncClient, class_db: AsyncSession):
        slug = TestDeleteLifecycle.api_slug
        service_id = TestDeleteLifecycle.api_service_id

        resp = await class_client.delete(
            f"/api/v1/services/{slug}",
            headers={"X-Edit-Token": TestDeleteLifecycle.api_token},
        )
        assert resp.status_code == 200
        assert resp.json() == {"deleted": slug}

        svc = (await class_db.execute(
            select(Service).where(Service.slug == slug)
        )).scalars().first()
        assert svc is None

        ratings = (await class_db.execute(
            select(Rating).where(Rating.service_id == service_id)
        )).scalars().all()
        assert ratings == []

    @pytest.mark.asyncio
    async def test_8_no_test_services_remain(self, class_client: AsyncClient, class_db: AsyncSession):
        """After deletes, zero services should be left in the DB."""
        count = (await class_db.execute(select(func.count(Service.id)))).scalar()
        assert count == 0
