import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Rating


class TestDirectory:
    @pytest.mark.asyncio
    async def test_homepage_returns_200(self, client: AsyncClient):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "L402 Service Directory" in resp.text

    @pytest.mark.asyncio
    async def test_homepage_shows_services(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_category_filter(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/?category=ai-ml")
        assert resp.status_code == 200
        assert "Test API" in resp.text

    @pytest.mark.asyncio
    async def test_category_filter_no_match(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/?category=finance")
        assert resp.status_code == 200
        assert "Test API" not in resp.text

    @pytest.mark.asyncio
    async def test_category_tabs_rendered(self, client: AsyncClient):
        resp = await client.get("/")
        assert "AI / ML" in resp.text
        assert "Finance" in resp.text
        assert "Tools" in resp.text


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
        assert "https://api.test.com" in resp.text
        assert "100 sats" in resp.text

    @pytest.mark.asyncio
    async def test_detail_404(self, client: AsyncClient):
        resp = await client.get("/services/no-such-service")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_detail_shows_categories(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/services/test-api")
        assert "AI / ML" in resp.text
        assert "Tools" in resp.text

    @pytest.mark.asyncio
    async def test_detail_shows_ratings(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/services/test-api")
        assert "Alice" in resp.text
        assert "Excellent" in resp.text


class TestSubmitService:
    @pytest.mark.asyncio
    async def test_submit_form_renders(self, client: AsyncClient):
        resp = await client.get("/submit")
        assert resp.status_code == 200
        assert "Submit a Service" in resp.text

    @pytest.mark.asyncio
    async def test_submit_creates_service(self, client: AsyncClient, db: AsyncSession):
        resp = await client.post("/submit", data={
            "name": "New Service",
            "url": "https://new.example.com",
            "description": "Brand new",
            "protocol": "L402",
            "pricing_sats": "50",
            "pricing_model": "per-request",
        }, follow_redirects=False)
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
    async def test_duplicate_name_gets_unique_slug(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        resp = await client.post("/submit", data={
            "name": "Test API",
            "url": "https://other.example.com",
        }, follow_redirects=False)
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
