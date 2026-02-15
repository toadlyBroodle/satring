"""Tests for edit token utilities, web edit flow, and API PATCH flow."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service
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
        resp = await client.post(f"/services/{svc.slug}/edit", data={
            "edit_token": token,
            "name": "Updated Name",
            "description": "Updated desc",
            "protocol": "X402",
            "pricing_sats": "200",
            "pricing_model": "flat",
            "owner_name": "New Owner",
            "owner_contact": "new@example.com",
            "logo_url": "",
        }, follow_redirects=False)
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
        resp = await client.post("/submit", data={
            "name": "Token Display Test",
            "url": "https://tokendisplay.example.com",
        }, follow_redirects=False)
        assert resp.status_code == 200
        assert "Token Display Test" in resp.text
        # The token should be displayed somewhere on the page
        assert "edit-token" in resp.text
