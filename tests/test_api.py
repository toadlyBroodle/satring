import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Rating


class TestListServices:
    @pytest.mark.asyncio
    async def test_empty_list(self, client: AsyncClient):
        resp = await client.get("/api/v1/services")
        assert resp.status_code == 200
        data = resp.json()
        assert data["services"] == []
        assert data["total"] == 0
        assert data["page"] == 1

    @pytest.mark.asyncio
    async def test_list_with_service(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services")
        data = resp.json()
        assert data["total"] == 1
        assert data["services"][0]["name"] == "Test API"
        assert data["services"][0]["slug"] == "test-api"
        assert len(data["services"][0]["categories"]) == 2

    @pytest.mark.asyncio
    async def test_category_filter(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services?category=ai-ml")
        data = resp.json()
        assert data["total"] == 1

        resp = await client.get("/api/v1/services?category=finance")
        data = resp.json()
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_pagination(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services?page=1&page_size=1")
        data = resp.json()
        assert data["page"] == 1
        assert data["page_size"] == 1
        assert len(data["services"]) == 1

        resp = await client.get("/api/v1/services?page=2&page_size=1")
        data = resp.json()
        assert len(data["services"]) == 0


class TestGetService:
    @pytest.mark.asyncio
    async def test_get_by_slug(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/test-api")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test API"
        assert data["url"] == "https://api.test.com"
        assert data["protocol"] == "L402"
        assert data["pricing_sats"] == 100

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, client: AsyncClient):
        resp = await client.get("/api/v1/services/nope")
        assert resp.status_code == 404


class TestCreateService:
    @pytest.mark.asyncio
    async def test_create_minimal(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Minimal API",
            "url": "https://min.example.com",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Minimal API"
        assert data["slug"] == "minimal-api"
        assert data["pricing_sats"] == 0
        assert data["protocol"] == "L402"

    @pytest.mark.asyncio
    async def test_create_full(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Full API",
            "url": "https://full.example.com",
            "description": "Fully specified",
            "pricing_sats": 500,
            "pricing_model": "per-minute",
            "protocol": "X402",
            "owner_name": "Builder",
            "owner_contact": "builder@test.com",
            "logo_url": "https://img.test.com/logo.png",
            "category_ids": [1, 2],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["pricing_sats"] == 500
        assert data["protocol"] == "X402"
        assert len(data["categories"]) == 2

    @pytest.mark.asyncio
    async def test_create_missing_name(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "url": "https://example.com",
        })
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_missing_url(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "No URL",
        })
        assert resp.status_code == 422


class TestSearchAPI:
    @pytest.mark.asyncio
    async def test_search_by_name(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=Test")
        data = resp.json()
        assert data["total"] == 1
        assert data["services"][0]["name"] == "Test API"

    @pytest.mark.asyncio
    async def test_search_by_description(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=Lightning")
        data = resp.json()
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_search_no_results(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=zzzznotfound")
        data = resp.json()
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_search_empty_query(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=")
        data = resp.json()
        assert data["total"] == 1

    @pytest.mark.asyncio
    async def test_search_pagination(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/search?q=Test&page=1&page_size=1")
        data = resp.json()
        assert data["page"] == 1
        assert data["page_size"] == 1


class TestRatingsAPI:
    @pytest.mark.asyncio
    async def test_list_ratings_empty(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/test-api/ratings")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_create_rating(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={
            "score": 4,
            "comment": "Great stuff",
            "reviewer_name": "Tester",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["score"] == 4
        assert data["comment"] == "Great stuff"
        assert data["reviewer_name"] == "Tester"

    @pytest.mark.asyncio
    async def test_create_rating_updates_service(self, client: AsyncClient, sample_service: Service, db: AsyncSession):
        await client.post("/api/v1/services/test-api/ratings", json={"score": 5})
        await client.post("/api/v1/services/test-api/ratings", json={"score": 3})

        await db.refresh(sample_service)
        assert sample_service.rating_count == 2
        assert sample_service.avg_rating == 4.0

    @pytest.mark.asyncio
    async def test_create_rating_invalid_score(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={"score": 0})
        assert resp.status_code == 422

        resp = await client.post("/api/v1/services/test-api/ratings", json={"score": 6})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_rating_nonexistent_service(self, client: AsyncClient):
        resp = await client.post("/api/v1/services/nope/ratings", json={"score": 3})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_list_ratings_with_data(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/ratings")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 3
        # Should be ordered by created_at desc
        names = [r["reviewer_name"] for r in data]
        assert "Alice" in names
        assert "Bob" in names
        assert "Charlie" in names

    @pytest.mark.asyncio
    async def test_default_reviewer_name(self, client: AsyncClient, sample_service: Service):
        resp = await client.post("/api/v1/services/test-api/ratings", json={"score": 3})
        assert resp.status_code == 201
        assert resp.json()["reviewer_name"] == "Anonymous"


class TestPremiumEndpoints:
    """Premium endpoints are L402-gated, but test-mode is active by default."""

    @pytest.mark.asyncio
    async def test_bulk_export(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/bulk")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "Test API"

    @pytest.mark.asyncio
    async def test_bulk_export_empty(self, client: AsyncClient):
        resp = await client.get("/api/v1/services/bulk")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_analytics(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/analytics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_services"] == 1
        assert data["total_ratings"] == 3
        assert data["pricing"]["avg_sats"] == 100.0
        assert len(data["top_rated"]) == 1

    @pytest.mark.asyncio
    async def test_analytics_empty(self, client: AsyncClient):
        resp = await client.get("/api/v1/analytics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_services"] == 0
        assert data["total_ratings"] == 0

    @pytest.mark.asyncio
    async def test_reputation(self, client: AsyncClient, sample_service_with_ratings: Service):
        resp = await client.get("/api/v1/services/test-api/reputation")
        assert resp.status_code == 200
        data = resp.json()
        assert data["service"]["name"] == "Test API"
        assert data["rating_summary"]["avg_rating"] == 4.0
        assert data["rating_summary"]["rating_count"] == 3
        assert data["rating_summary"]["distribution"]["5"] == 1
        assert data["rating_summary"]["distribution"]["4"] == 1
        assert data["rating_summary"]["distribution"]["3"] == 1
        assert data["rating_summary"]["distribution"]["1"] == 0
        assert len(data["recent_reviews"]) == 3

    @pytest.mark.asyncio
    async def test_reputation_nonexistent(self, client: AsyncClient):
        resp = await client.get("/api/v1/services/nope/reputation")
        assert resp.status_code == 404


class TestSlugify:
    """Test slug generation via the API create endpoint."""

    @pytest.mark.asyncio
    async def test_basic_slugification(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "My Cool API Service",
            "url": "https://example.com",
        })
        assert resp.json()["slug"] == "my-cool-api-service"

    @pytest.mark.asyncio
    async def test_special_characters_stripped(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "API @ $pecial! (Chars)",
            "url": "https://example.com",
        })
        slug = resp.json()["slug"]
        assert "@" not in slug
        assert "$" not in slug
        assert "!" not in slug

    @pytest.mark.asyncio
    async def test_duplicate_name_unique_slug(self, client: AsyncClient):
        resp1 = await client.post("/api/v1/services", json={
            "name": "Duplicate Name",
            "url": "https://a.example.com",
        })
        resp2 = await client.post("/api/v1/services", json={
            "name": "Duplicate Name",
            "url": "https://b.example.com",
        })
        slug1 = resp1.json()["slug"]
        slug2 = resp2.json()["slug"]
        assert slug1 != slug2
        assert slug2.startswith("duplicate-name-")
