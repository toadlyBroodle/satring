import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Rating


class TestDirectory:
    @pytest.mark.asyncio
    async def test_homepage_returns_200(self, client: AsyncClient):
        resp = await client.get("/directory")
        assert resp.status_code == 200
        assert "API Directory" in resp.text

    @pytest.mark.asyncio
    async def test_homepage_shows_services(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/directory")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_default_sort_is_popular(self, client: AsyncClient, sample_service: Service):
        """Directory default sort should be popular (hit_count_30d desc)."""
        resp = await client.get("/directory")
        assert resp.status_code == 200
        # Popular button should be active by default (no sort param)
        assert 'active">[popular]' in resp.text or 'active"><span class="bracket">[</span>popular' in resp.text

    @pytest.mark.asyncio
    async def test_sort_popular(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/directory?sort=popular")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_category_filter(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/directory?category=ai-ml")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_category_filter_no_match(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/directory?category=finance")
        assert resp.status_code == 200
        assert "Test API" not in resp.text

    @pytest.mark.asyncio
    async def test_category_tabs_rendered(self, client: AsyncClient):
        resp = await client.get("/directory")
        assert "ai/ml" in resp.text
        assert "finance" in resp.text
        assert "tools" in resp.text


    @pytest.mark.asyncio
    async def test_protocol_filter_includes_dual(self, client: AsyncClient, sample_service: Service, sample_dual_service: Service):
        # L402 filter should show both L402 and L402+x402 services
        resp = await client.get("/directory?protocol=L402")
        assert resp.status_code == 200
        assert "Test API" in resp.text
        assert "Dual Protocol API" in resp.text

    @pytest.mark.asyncio
    async def test_protocol_filter_dual_only(self, client: AsyncClient, sample_service: Service, sample_dual_service: Service):
        resp = await client.get("/directory?protocol=L402%2Bx402")
        assert resp.status_code == 200
        assert "Test API" not in resp.text
        assert "Dual Protocol API" in resp.text


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_matching(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/search?q=Test")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_search_no_match(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/search?q=nonexistent")
        assert resp.status_code == 200
        assert "Test API" not in resp.text

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_all(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/search?q=")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_search_matches_description(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/search?q=Lightning")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_search_is_case_insensitive(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/search?q=test+api")
        assert resp.status_code == 200
        assert "Test API" in resp.text


class TestServiceDetail:
    @pytest.mark.asyncio
    async def test_detail_page(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert resp.status_code == 200
        assert "Test API" in resp.text
        # Endpoint URL is loaded via JS (meta.json), not in static HTML
        assert "meta.json" in resp.text
        assert "100 sats" in resp.text

    @pytest.mark.asyncio
    async def test_detail_html_does_not_leak_url(self, client: AsyncClient, sample_service: Service):
        """Endpoint URL must not appear in static HTML (only via JS meta.json)."""
        resp = await client.get("/services/test-api")
        assert "https://api.test.com" not in resp.text

    @pytest.mark.asyncio
    async def test_meta_json_returns_url_with_referer(self, client: AsyncClient, sample_service: Service):
        resp = await client.get(
            "/services/test-api/meta.json",
            headers={"Referer": "https://satring.com/services/test-api"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://api.test.com"

    @pytest.mark.asyncio
    async def test_meta_json_blocked_without_referer(self, client: AsyncClient, sample_service: Service):
        """Direct meta.json requests without valid Referer should be blocked."""
        resp = await client.get("/services/test-api/meta.json")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_meta_json_blocked_with_wrong_referer(self, client: AsyncClient, sample_service: Service):
        resp = await client.get(
            "/services/test-api/meta.json",
            headers={"Referer": "https://evil.com/scrape"},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_meta_json_404_for_missing(self, client: AsyncClient):
        resp = await client.get(
            "/services/no-such-service/meta.json",
            headers={"Referer": "https://satring.com/services/no-such-service"},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_404(self, client: AsyncClient):
        resp = await client.get("/services/no-such-service")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_shows_categories(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert "ai/ml" in resp.text
        assert "tools" in resp.text

    @pytest.mark.asyncio
    async def test_detail_shows_ratings(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/services/test-api")
        assert "Alice" in resp.text
        assert "Excellent" in resp.text


    @pytest.mark.asyncio
    async def test_detail_dual_protocol_shows_x402_fields(self, client: AsyncClient, sample_dual_service: Service):
        """x402 fields are loaded via JS (meta.json), not in static HTML."""
        resp = await client.get("/services/dual-proto-api")
        assert resp.status_code == 200
        assert "L402+x402" in resp.text
        # Wallet/network now loaded via meta.json, not embedded in HTML
        assert "0xDualWallet456" not in resp.text
        # But the meta.json endpoint returns them (with valid Referer)
        meta = await client.get(
            "/services/dual-proto-api/meta.json",
            headers={"Referer": "https://satring.com/services/dual-proto-api"},
        )
        data = meta.json()
        assert data["x402_pay_to"] == "0xDualWallet456"
        assert data["x402_network"] == "eip155:8453"


class TestOwnerDashboard:
    @pytest.mark.asyncio
    async def test_owner_dashboard_renders(self, client: AsyncClient, sample_service: Service):
        """Owner dashboard should render in test mode (no token required)."""
        resp = await client.get("/owner/api.test.com")
        assert resp.status_code == 200
        assert "Owner Dashboard" in resp.text or "api.test.com" in resp.text

    @pytest.mark.asyncio
    async def test_owner_dashboard_404_unknown_domain(self, client: AsyncClient):
        resp = await client.get("/owner/nonexistent.example.com")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_owner_traffic_api(self, client: AsyncClient, sample_service: Service):
        """Owner traffic API should return aggregated data in test mode."""
        resp = await client.get("/api/v1/owner/api.test.com/traffic")
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] == "api.test.com"
        assert data["service_count"] >= 1
        assert "total_hits" in data
        assert "hits_7d" in data
        assert "hits_30d" in data
        assert "unique_ips_30d" in data
        assert "services" in data
        assert "daily_hits_30d" in data

    @pytest.mark.asyncio
    async def test_owner_traffic_api_404(self, client: AsyncClient):
        resp = await client.get("/api/v1/owner/nonexistent.example.com/traffic")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_owner_audience_api(self, client: AsyncClient, sample_service: Service):
        """Owner audience should return 200 in test mode (payment bypassed)."""
        resp = await client.get("/api/v1/owner/api.test.com/audience")
        assert resp.status_code == 200
        data = resp.json()
        assert data["domain"] == "api.test.com"
        assert "source_breakdown" in data
        assert "top_routes_30d" in data
        assert "daily_unique_ips_30d" in data
        assert "agent_breakdown" in data
        assert "geo" in data

    @pytest.mark.asyncio
    async def test_owner_traffic_requires_token_when_payments_enabled(self, client: AsyncClient, sample_service: Service):
        """With payments enabled, owner traffic should require X-Edit-Token."""
        from unittest.mock import patch
        from app.config import settings
        with patch.object(settings, "AUTH_ROOT_KEY", "real-key"):
            resp = await client.get("/api/v1/owner/api.test.com/traffic")
            assert resp.status_code == 403


class TestSubmitService:
    @pytest.mark.asyncio
    async def test_submit_form_renders(self, client: AsyncClient):
        resp = await client.get("/submit")
        assert resp.status_code == 200
        assert "Submit a Service" in resp.text

    @pytest.mark.asyncio
    async def test_submit_creates_service(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", content="name=New+Service&url=https%3A%2F%2Fnew.example.com&description=Brand+new&protocol=L402&pricing_sats=50&pricing_model=per-request&categories=9", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200
        assert "New Service" in resp.text
        assert "edit token" in resp.text.lower() or "edit-token" in resp.text.lower()

        result = await db.execute(select(Service).where(Service.slug == "new-service"))
        svc = result.scalars().first()
        assert svc is not None
        assert svc.name == "New Service"
        assert svc.pricing_sats == 50
        assert svc.edit_token_hash is not None

    @pytest.mark.asyncio
    async def test_submit_with_categories(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", content="name=Cat+Service&url=https%3A%2F%2Fcat.example.com&categories=1&categories=2", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200

        result = await db.execute(select(Service).where(Service.slug == "cat-service"))
        svc = result.scalars().first()
        assert svc is not None

    @pytest.mark.asyncio
    async def test_submit_dual_protocol(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", content="name=Dual+Submit&url=https%3A%2F%2Fdual-submit.example.com&protocol=L402%2Bx402&pricing_sats=100&pricing_model=per-request&x402_pay_to=0xWallet&x402_network=eip155%3A8453&pricing_usd=0.05&categories=9", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200

        result = await db.execute(select(Service).where(Service.slug == "dual-submit"))
        svc = result.scalars().first()
        assert svc is not None
        assert svc.protocol == "L402+x402"
        assert svc.x402_pay_to == "0xWallet"

    @pytest.mark.asyncio
    async def test_duplicate_name_gets_unique_slug(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        resp = await client.post("/submit", content="name=Test+API&url=https%3A%2F%2Fother.example.com&categories=9", headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert resp.status_code == 200

        # Should have created a service with a different slug
        result = await db.execute(select(Service).where(Service.slug.like("test-api-%")))
        svc = result.scalars().first()
        assert svc is not None
        assert svc.slug != "test-api"


class TestRateService:
    @pytest.mark.asyncio
    async def test_rate_returns_review_bubble(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/services/test-api/rate", data={
            "score": "5",
            "comment": "Amazing service",
            "reviewer_name": "Reviewer",
        })
        assert resp.status_code == 200
        assert "Amazing service" in resp.text
        assert "Reviewer" in resp.text

    @pytest.mark.asyncio
    async def test_rate_updates_avg_rating(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        await client.post("/services/test-api/rate", data={"score": "4", "comment": ""})
        await client.post("/services/test-api/rate", data={"score": "2", "comment": ""})

        await db.refresh(sample_service)
        assert sample_service.rating_count == 2
        assert sample_service.avg_rating == 3.0

    @pytest.mark.asyncio
    async def test_rate_clamps_score(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        resp = await client.post("/services/test-api/rate", data={"score": "10"})
        assert resp.status_code == 200

        result = await db.execute(select(Rating).where(Rating.service_id == sample_service.id))
        rating = result.scalars().first()
        assert rating.score == 5

    @pytest.mark.asyncio
    async def test_rate_nonexistent_service(self, client: AsyncClient):
        resp = await client.post("/services/no-such/rate", data={"score": "3"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_rate_default_reviewer_name(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        await client.post("/services/test-api/rate", data={"score": "3", "reviewer_name": ""})
        result = await db.execute(select(Rating).where(Rating.service_id == sample_service.id))
        rating = result.scalars().first()
        assert rating.reviewer_name == "Anonymous"
