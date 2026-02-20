"""Tests for security fixes: XSS, SSRF, CSRF, input length limits, rate limiting."""
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    MAX_NAME, MAX_URL, MAX_DESCRIPTION, MAX_OWNER_NAME,
    MAX_OWNER_CONTACT, MAX_LOGO_URL, MAX_REVIEWER_NAME, MAX_COMMENT,
)
from app.models import Service
from app.utils import is_public_hostname


# ---------------------------------------------------------------------------
# 1. XSS: URL scheme validation
# ---------------------------------------------------------------------------

class TestURLSchemeValidation:
    """Reject non-http(s) schemes to prevent stored XSS via javascript:/data: URIs."""

    @pytest.mark.asyncio
    async def test_web_rejects_javascript_url(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Evil", "url": "javascript:alert(1)",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422
        assert "http" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_web_rejects_data_url(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Evil", "url": "data:text/html,<script>alert(1)</script>",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_web_accepts_https_url(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", data={
            "name": "Good", "url": "https://good.example.com",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 200
        assert "Good" in resp.text

    @pytest.mark.asyncio
    async def test_web_accepts_http_url(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Plain HTTP", "url": "http://plain.example.com",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_rejects_javascript_url(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Evil", "url": "javascript:alert(1)",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_web_rejects_no_scheme(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "NoScheme", "url": "not-a-url",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 2. XSS: logo_url scheme validation
# ---------------------------------------------------------------------------

class TestLogoURLValidation:
    """Reject non-http(s) logo URLs."""

    @pytest.mark.asyncio
    async def test_web_rejects_javascript_logo(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "LogoTest", "url": "https://example.com",
            "logo_url": "javascript:alert(1)",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_web_rejects_data_logo(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "LogoTest", "url": "https://example.com",
            "logo_url": "data:image/svg+xml,<svg onload=alert(1)>",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_web_accepts_empty_logo(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "NoLogo", "url": "https://example.com",
            "logo_url": "", "description": "", "categories": "1",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_web_accepts_https_logo(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "GoodLogo", "url": "https://example.com",
            "logo_url": "https://img.example.com/logo.png",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_api_rejects_javascript_logo(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Evil Logo", "url": "https://example.com",
            "logo_url": "javascript:alert(1)",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_api_accepts_empty_logo(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "No Logo API", "url": "https://example.com",
            "logo_url": "",
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 3. SSRF: is_public_hostname
# ---------------------------------------------------------------------------

class TestSSRFProtection:
    """Block private/reserved IPs to prevent SSRF."""

    def test_loopback_blocked(self):
        assert is_public_hostname("127.0.0.1") is False

    def test_loopback_v6_blocked(self):
        assert is_public_hostname("::1") is False

    def test_link_local_blocked(self):
        assert is_public_hostname("169.254.169.254") is False

    def test_private_10_blocked(self):
        assert is_public_hostname("10.0.0.1") is False

    def test_private_172_blocked(self):
        assert is_public_hostname("172.16.0.1") is False

    def test_private_192_blocked(self):
        assert is_public_hostname("192.168.1.1") is False

    def test_public_ip_allowed(self):
        assert is_public_hostname("8.8.8.8") is True

    def test_unresolvable_hostname_blocked(self):
        assert is_public_hostname("this.host.does.not.exist.invalid") is False

    def test_empty_string_blocked(self):
        assert is_public_hostname("") is False

    def test_zero_ip_blocked(self):
        assert is_public_hostname("0.0.0.0") is False


# ---------------------------------------------------------------------------
# 4. CSRF: Origin header check
# ---------------------------------------------------------------------------

class TestOriginCheck:
    """Reject cross-origin POST requests."""

    @pytest.mark.asyncio
    async def test_cross_origin_post_blocked(self, client: AsyncClient, sample_service: Service):
        resp = await client.post(
            "/services/test-api/rate",
            data={"score": "5", "comment": "spam"},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_cross_origin_api_post_blocked(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/services",
            json={"name": "Evil", "url": "https://example.com"},
            headers={"Origin": "https://evil.com"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cross-origin request blocked"

    @pytest.mark.asyncio
    async def test_same_origin_post_allowed(self, client: AsyncClient, sample_service: Service):
        resp = await client.post(
            "/services/test-api/rate",
            data={"score": "5", "comment": "legit"},
            headers={"Origin": "https://satring.com"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_no_origin_header_allowed(self, client: AsyncClient, sample_service: Service):
        """Requests without Origin header (e.g. curl) should be allowed."""
        resp = await client.post(
            "/services/test-api/rate",
            data={"score": "5", "comment": "curl"},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_get_requests_unaffected(self, client: AsyncClient):
        """GET requests should not be blocked regardless of Origin."""
        resp = await client.get("/", headers={"Origin": "https://evil.com"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 5. Input length limits — web form
# ---------------------------------------------------------------------------

class TestWebInputLengthLimits:
    """Server-side length validation on the web submit form."""

    @pytest.mark.asyncio
    async def test_name_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "x" * (MAX_NAME + 1), "url": "https://example.com",
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422
        assert "name" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_description_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Test", "url": "https://example.com",
            "description": "x" * (MAX_DESCRIPTION + 1), "categories": "1",
        })
        assert resp.status_code == 422
        assert "description" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_url_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Test", "url": "https://example.com/" + "x" * MAX_URL,
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_owner_name_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Test", "url": "https://example.com",
            "owner_name": "x" * (MAX_OWNER_NAME + 1),
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_owner_contact_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Test", "url": "https://example.com",
            "owner_contact": "x" * (MAX_OWNER_CONTACT + 1),
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_logo_url_too_long(self, client: AsyncClient):
        resp = await client.post("/submit", data={
            "name": "Test", "url": "https://example.com",
            "logo_url": "https://example.com/" + "x" * MAX_LOGO_URL,
            "description": "", "categories": "1",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_at_max_length_accepted(self, client: AsyncClient):
        """Values exactly at the limit should be accepted."""
        resp = await client.post("/submit", data={
            "name": "x" * MAX_NAME, "url": "https://example.com",
            "description": "x" * MAX_DESCRIPTION, "categories": "1",
        })
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 6. Input length limits — web rating form
# ---------------------------------------------------------------------------

class TestWebRatingLengthLimits:
    """Server-side length validation on the web rating form."""

    @pytest.mark.asyncio
    async def test_reviewer_name_too_long(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/services/test-api/rate", data={
            "score": "5", "reviewer_name": "x" * (MAX_REVIEWER_NAME + 1),
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_comment_too_long(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/services/test-api/rate", data={
            "score": "5", "comment": "x" * (MAX_COMMENT + 1),
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_at_max_length_accepted(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/services/test-api/rate", data={
            "score": "5",
            "reviewer_name": "x" * MAX_REVIEWER_NAME,
            "comment": "x" * MAX_COMMENT,
        })
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 7. Input length limits — API
# ---------------------------------------------------------------------------

class TestAPIInputLengthLimits:
    """Pydantic max_length enforcement on API models."""

    @pytest.mark.asyncio
    async def test_name_too_long(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "x" * (MAX_NAME + 1), "url": "https://example.com",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_name_rejected(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "", "url": "https://example.com",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_description_too_long(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Test", "url": "https://example.com",
            "description": "x" * (MAX_DESCRIPTION + 1),
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_logo_url_too_long(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Test", "url": "https://example.com",
            "logo_url": "https://example.com/" + "x" * MAX_LOGO_URL,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_reviewer_name_too_long(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={
            "score": 5, "reviewer_name": "x" * (MAX_REVIEWER_NAME + 1),
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_comment_too_long(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={
            "score": 5, "comment": "x" * (MAX_COMMENT + 1),
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_pricing_sats_capped(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Expensive", "url": "https://example.com",
            "pricing_sats": 999_999_999,
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_at_max_length_accepted(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "x" * MAX_NAME, "url": "https://example.com",
            "description": "x" * MAX_DESCRIPTION,
            "owner_name": "x" * MAX_OWNER_NAME,
            "owner_contact": "x" * MAX_OWNER_CONTACT,
        })
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# 8. Rate limiting
# ---------------------------------------------------------------------------

class TestRateLimiting:
    """Verify slowapi rate limits trigger on excessive requests."""

    @pytest.mark.asyncio
    async def test_payment_status_rate_limited(self, client: AsyncClient):
        """Payment status at 60/min — 65 requests should hit the limit."""
        last_status = None
        for _ in range(65):
            resp = await client.get("/payment-status/fakehash")
            last_status = resp.status_code
            if last_status == 429:
                break
        assert last_status == 429

    @pytest.mark.asyncio
    async def test_search_rate_limited(self, client: AsyncClient):
        """Search at 30/min — 35 requests should hit the limit."""
        last_status = None
        for _ in range(35):
            resp = await client.get("/search?q=test")
            last_status = resp.status_code
            if last_status == 429:
                break
        assert last_status == 429
