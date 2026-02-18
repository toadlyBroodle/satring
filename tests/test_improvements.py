"""Tests for the 4 improvements: payment replay fix, pagination, loading indicators, SEO."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Category, Rating, ConsumedPayment


# ---------------------------------------------------------------------------
# 1. Payment Replay Fix — DB persistence
# ---------------------------------------------------------------------------

class TestConsumedPaymentModel:
    @pytest.mark.asyncio
    async def test_consumed_payment_persists(self, db: AsyncSession):
        """Payment hash stored in DB survives across queries (not just in-memory)."""
        from app.l402 import check_and_consume_payment

        assert await check_and_consume_payment("persist-hash-1", db) is True
        await db.commit()

        # Query the table directly to confirm it's in the DB
        result = await db.execute(
            select(ConsumedPayment).where(ConsumedPayment.payment_hash == "persist-hash-1")
        )
        consumed = result.scalars().first()
        assert consumed is not None
        assert consumed.payment_hash == "persist-hash-1"
        assert consumed.consumed_at is not None

    @pytest.mark.asyncio
    async def test_consumed_payment_rejects_duplicate(self, db: AsyncSession):
        """Second attempt with same hash is rejected via DB constraint."""
        from app.l402 import check_and_consume_payment

        assert await check_and_consume_payment("dup-hash-1", db) is True
        await db.commit()
        assert await check_and_consume_payment("dup-hash-1", db) is False


# ---------------------------------------------------------------------------
# 2. Pagination — Web directory
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def many_services(db: AsyncSession):
    """Create 25 services to test pagination (PAGE_SIZE = 20)."""
    cats = (await db.execute(select(Category).where(Category.slug == "tools"))).scalars().all()
    services = []
    for i in range(25):
        svc = Service(
            name=f"Service {i:02d}",
            slug=f"service-{i:02d}",
            url=f"https://svc-{i:02d}.example.com",
            description=f"Service number {i}",
            pricing_sats=i * 10,
            protocol="L402",
        )
        svc.categories = list(cats)
        db.add(svc)
        services.append(svc)
    await db.commit()
    return services


class TestWebPagination:
    @pytest.mark.asyncio
    async def test_directory_page_1_default(self, client: AsyncClient, many_services):
        resp = await client.get("/")
        assert resp.status_code == 200
        # Should show page controls since we have > 20 services
        assert ">1</span>/2</span>" in resp.text
        assert "next" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_directory_page_2(self, client: AsyncClient, many_services):
        resp = await client.get("/?page=2")
        assert resp.status_code == 200
        assert ">2</span>/2</span>" in resp.text

    @pytest.mark.asyncio
    async def test_directory_page_out_of_range_clamps(self, client: AsyncClient, many_services):
        resp = await client.get("/?page=999")
        assert resp.status_code == 200
        # Should clamp to last page
        assert ">2</span>/2</span>" in resp.text

    @pytest.mark.asyncio
    async def test_directory_no_pagination_when_few(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/")
        assert resp.status_code == 200
        # Only 1 service, should NOT have prev/next links
        assert "prev]</a>" not in resp.text
        assert "next" not in resp.text.lower() or "next &gt;]</a>" not in resp.text

    @pytest.mark.asyncio
    async def test_directory_pagination_preserves_filters(self, client: AsyncClient, many_services):
        resp = await client.get("/?category=tools&page=1")
        assert resp.status_code == 200
        # Pagination links should include the category filter
        assert "category=tools" in resp.text

    @pytest.mark.asyncio
    async def test_search_returns_paginated(self, client: AsyncClient, many_services):
        resp = await client.get("/search?q=Service")
        assert resp.status_code == 200
        # Search results should include pagination if > 20 matches
        assert "/2</span>" in resp.text

    @pytest.mark.asyncio
    async def test_search_page_2(self, client: AsyncClient, many_services):
        resp = await client.get("/search?q=Service&page=2")
        assert resp.status_code == 200
        assert ">2</span>/2</span>" in resp.text


# ---------------------------------------------------------------------------
# 2b. Pagination — API ratings limit/offset
# ---------------------------------------------------------------------------

class TestAPIRatingsLimitOffset:
    @pytest.mark.asyncio
    async def test_ratings_default_limit(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3  # Only 3 ratings exist

    @pytest.mark.asyncio
    async def test_ratings_limit_1(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings?limit=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_ratings_offset(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings?limit=1&offset=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_ratings_offset_past_end(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings?offset=100")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 3. Loading Indicators — HTML element presence
# ---------------------------------------------------------------------------

class TestLoadingIndicators:
    @pytest.mark.asyncio
    async def test_search_has_htmx_indicator(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert 'id="search-indicator"' in resp.text
        assert "htmx-indicator" in resp.text

    @pytest.mark.asyncio
    async def test_review_form_has_disabled_elt(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert resp.status_code == 200
        assert "hx-disabled-elt" in resp.text
        assert 'id="review-submit-btn"' in resp.text
        assert 'id="review-indicator"' in resp.text

    @pytest.mark.asyncio
    async def test_submit_form_has_disable_js(self, client: AsyncClient):
        resp = await client.get("/submit")
        assert resp.status_code == 200
        assert 'id="submit-btn"' in resp.text
        assert "SUBMITTING" in resp.text


# ---------------------------------------------------------------------------
# 4. SEO — Meta tags, OG tags, sitemap, robots, llms.txt
# ---------------------------------------------------------------------------

class TestSEOMeta:
    @pytest.mark.asyncio
    async def test_homepage_has_meta_description(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert '<meta name="description"' in resp.text

    @pytest.mark.asyncio
    async def test_homepage_has_og_tags(self, client: AsyncClient):
        resp = await client.get("/")
        assert 'og:title' in resp.text
        assert 'og:description' in resp.text
        assert 'og:type' in resp.text

    @pytest.mark.asyncio
    async def test_homepage_has_twitter_card(self, client: AsyncClient):
        resp = await client.get("/")
        assert 'twitter:card' in resp.text
        assert 'twitter:title' in resp.text

    @pytest.mark.asyncio
    async def test_detail_has_service_specific_og(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert resp.status_code == 200
        # Should contain service name in OG tags
        assert "Test API" in resp.text
        assert 'og:type' in resp.text

    @pytest.mark.asyncio
    async def test_detail_has_json_ld(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert 'application/ld+json' in resp.text
        assert 'SoftwareApplication' in resp.text

    @pytest.mark.asyncio
    async def test_detail_json_ld_has_aggregate_rating(
        self, client: AsyncClient, sample_service_with_ratings: Service,
    ):
        resp = await client.get("/services/test-api")
        assert 'aggregateRating' in resp.text
        assert 'ratingCount' in resp.text

    @pytest.mark.asyncio
    async def test_detail_json_ld_no_rating_when_zero(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert 'aggregateRating' not in resp.text


class TestSitemap:
    @pytest.mark.asyncio
    async def test_sitemap_returns_xml(self, client: AsyncClient):
        resp = await client.get("/sitemap.xml")
        assert resp.status_code == 200
        assert "application/xml" in resp.headers["content-type"]
        assert '<?xml version="1.0"' in resp.text
        assert "<urlset" in resp.text

    @pytest.mark.asyncio
    async def test_sitemap_includes_site_pages(self, client: AsyncClient):
        resp = await client.get("/sitemap.xml")
        assert "/</loc>" in resp.text
        assert "/submit</loc>" in resp.text
        assert "/docs</loc>" in resp.text

    @pytest.mark.asyncio
    async def test_sitemap_only_own_pages(self, client: AsyncClient, sample_service: Service):
        """Sitemap lists satring's own pages, not user-submitted service listings."""
        resp = await client.get("/sitemap.xml")
        assert "/services/" not in resp.text
        assert "/api/" not in resp.text


class TestRobotsTxt:
    @pytest.mark.asyncio
    async def test_robots_returns_text(self, client: AsyncClient):
        resp = await client.get("/robots.txt")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_robots_has_sitemap(self, client: AsyncClient):
        resp = await client.get("/robots.txt")
        assert "Sitemap:" in resp.text
        assert "sitemap.xml" in resp.text

    @pytest.mark.asyncio
    async def test_robots_has_llms_txt(self, client: AsyncClient):
        resp = await client.get("/robots.txt")
        assert "llms.txt" in resp.text


class TestLlmsTxt:
    @pytest.mark.asyncio
    async def test_llms_txt_returns_text(self, client: AsyncClient):
        resp = await client.get("/llms.txt")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]

    @pytest.mark.asyncio
    async def test_llms_txt_has_required_h1(self, client: AsyncClient):
        resp = await client.get("/llms.txt")
        assert resp.text.startswith("# satring")

    @pytest.mark.asyncio
    async def test_llms_txt_has_blockquote(self, client: AsyncClient):
        resp = await client.get("/llms.txt")
        assert "> " in resp.text

    @pytest.mark.asyncio
    async def test_llms_txt_lists_categories(self, client: AsyncClient):
        resp = await client.get("/llms.txt")
        assert "## Categories" in resp.text
        assert "AI / ML" in resp.text
        assert "Finance" in resp.text

    @pytest.mark.asyncio
    async def test_llms_txt_has_api_section(self, client: AsyncClient):
        resp = await client.get("/llms.txt")
        assert "## API" in resp.text
        assert "/api/v1/services" in resp.text

    @pytest.mark.asyncio
    async def test_llms_txt_does_not_dump_services(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/llms.txt")
        assert "Test API" not in resp.text
        assert "## Services" not in resp.text
