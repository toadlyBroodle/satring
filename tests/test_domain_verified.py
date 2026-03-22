"""Tests for the domain_verified badge feature."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service
from app.utils import generate_edit_token, hash_token, overwrite_purged_service


async def _create_service(db: AsyncSession, slug: str, url: str = "https://ep.example.com") -> tuple[Service, str]:
    token = generate_edit_token()
    svc = Service(
        name=f"Service {slug}", slug=slug, url=url,
        description="Test service", pricing_sats=42,
        pricing_model="per-request", protocol="L402",
        edit_token_hash=hash_token(token),
    )
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc, token


class TestDomainVerifiedDefault:
    @pytest.mark.asyncio
    async def test_new_service_not_verified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "unverified-test")
        assert svc.domain_verified is False

    @pytest.mark.asyncio
    async def test_api_create_returns_domain_verified_false(self, client: AsyncClient):
        resp = await client.post("/api/v1/services", json={
            "name": "Fresh Service",
            "url": "https://fresh.example.com",
        })
        assert resp.status_code == 201
        assert resp.json()["domain_verified"] is False

    @pytest.mark.asyncio
    async def test_api_get_includes_domain_verified(self, client: AsyncClient, sample_service: Service):
        resp = await client.get("/api/v1/services/test-api")
        assert resp.status_code == 200
        data = resp.json()
        assert "domain_verified" in data
        assert data["domain_verified"] is False


class TestAPIVerifySetsFlag:
    @pytest.mark.asyncio
    async def test_api_verify_sets_domain_verified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "api-verify", url="https://verified.example.com")

        # Generate challenge
        gen_resp = await client.post(f"/api/v1/services/{svc.slug}/recover/generate")
        challenge = gen_resp.json()["challenge"]

        # Mock HTTP fetch to return correct challenge + bypass SSRF check
        with patch("app.routes.api.httpx.AsyncClient") as MockClient, \
             patch("app.routes.api.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post(f"/api/v1/services/{svc.slug}/recover/verify")
            assert resp.status_code == 200

        await db.refresh(svc)
        assert svc.domain_verified is True

    @pytest.mark.asyncio
    async def test_api_verify_sets_all_same_domain_services(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "domain-a1", url="https://verified.example.com/api1")
        svc2, _ = await _create_service(db, "domain-a2", url="https://verified.example.com/api2")

        # Generate challenge on svc1
        gen_resp = await client.post(f"/api/v1/services/{svc1.slug}/recover/generate")
        challenge = gen_resp.json()["challenge"]

        with patch("app.routes.api.httpx.AsyncClient") as MockClient, \
             patch("app.routes.api.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await client.post(f"/api/v1/services/{svc1.slug}/recover/verify")

        await db.refresh(svc1)
        await db.refresh(svc2)
        assert svc1.domain_verified is True
        assert svc2.domain_verified is True


class TestWebVerifySetsFlag:
    @pytest.mark.asyncio
    async def test_web_verify_sets_domain_verified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "web-verify", url="https://webverified.example.com")

        # Generate challenge via web
        await client.post(f"/services/{svc.slug}/recover", data={"action": "generate"})
        await db.refresh(svc)
        challenge = svc.domain_challenge

        # Verify via web — bypass SSRF check since hostname won't resolve in tests
        with patch("app.routes.web.httpx.AsyncClient") as MockClient, \
             patch("app.routes.web.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post(f"/services/{svc.slug}/recover", data={"action": "verify"})
            assert resp.status_code == 200

        await db.refresh(svc)
        assert svc.domain_verified is True

    @pytest.mark.asyncio
    async def test_web_verify_sets_all_same_domain_services(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "web-d1", url="https://webdomain.example.com/a")
        svc2, _ = await _create_service(db, "web-d2", url="https://webdomain.example.com/b")

        await client.post(f"/services/{svc1.slug}/recover", data={"action": "generate"})
        await db.refresh(svc1)
        challenge = svc1.domain_challenge

        with patch("app.routes.web.httpx.AsyncClient") as MockClient, \
             patch("app.routes.web.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await client.post(f"/services/{svc1.slug}/recover", data={"action": "verify"})

        await db.refresh(svc1)
        await db.refresh(svc2)
        assert svc1.domain_verified is True
        assert svc2.domain_verified is True


class TestVerifiedFilter:
    @pytest.mark.asyncio
    async def test_directory_verified_filter(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "filter-v", url="https://v.example.com")
        svc2, _ = await _create_service(db, "filter-u", url="https://u.example.com")
        svc1.domain_verified = True
        await db.commit()

        # Without filter — both show
        resp = await client.get("/directory")
        assert "Service filter-v" in resp.text
        assert "Service filter-u" in resp.text

        # With verified filter — only verified shows
        resp = await client.get("/directory?verified=true")
        assert "Service filter-v" in resp.text
        assert "Service filter-u" not in resp.text

    @pytest.mark.asyncio
    async def test_search_verified_filter(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "search-v", url="https://sv.example.com")
        svc2, _ = await _create_service(db, "search-u", url="https://su.example.com")
        svc1.domain_verified = True
        await db.commit()

        resp = await client.get("/search?q=Service&verified=true")
        assert "Service search-v" in resp.text
        assert "Service search-u" not in resp.text


class TestVerifiedBadgeUI:
    @pytest.mark.asyncio
    async def test_card_shows_verified_badge(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "badge-card", url="https://badge.example.com")
        svc.domain_verified = True
        await db.commit()

        resp = await client.get("/directory")
        assert "[verified]" in resp.text

    @pytest.mark.asyncio
    async def test_card_no_badge_when_unverified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "nobadge-card", url="https://nobadge.example.com")

        resp = await client.get("/directory")
        # The filter button text "[verified]" will be present, but not in the card context
        # Check detail page instead for clean assertion
        resp = await client.get(f"/services/{svc.slug}")
        assert "domain verified" not in resp.text

    @pytest.mark.asyncio
    async def test_detail_shows_verified_badge(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "badge-detail", url="https://bdetail.example.com")
        svc.domain_verified = True
        await db.commit()

        resp = await client.get(f"/services/{svc.slug}")
        assert "domain verified" in resp.text

    @pytest.mark.asyncio
    async def test_detail_shows_verify_button_when_unverified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "verify-btn", url="https://vbtn.example.com")

        resp = await client.get(f"/services/{svc.slug}")
        assert "VERIFY DOMAIN" in resp.text

    @pytest.mark.asyncio
    async def test_detail_hides_verify_button_when_verified(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "no-verify-btn", url="https://nvbtn.example.com")
        svc.domain_verified = True
        await db.commit()

        resp = await client.get(f"/services/{svc.slug}")
        assert "VERIFY DOMAIN" not in resp.text


class TestAutoVerifyOnCreate:
    """New services on already-verified domains should inherit verification."""

    @pytest.mark.asyncio
    async def test_api_create_auto_verifies_same_domain(self, client: AsyncClient, db: AsyncSession):
        # Create and verify a service on the domain
        svc1, _ = await _create_service(db, "auto-v-existing", url="https://autov.example.com/api1")
        svc1.domain_verified = True
        svc1.domain_challenge = "test-challenge-abc"
        await db.commit()

        # Create a second service on the same domain via API
        resp = await client.post("/api/v1/services", json={
            "name": "AutoVerify New",
            "url": "https://autov.example.com/api2",
        })
        assert resp.status_code == 201
        assert resp.json()["domain_verified"] is True

    @pytest.mark.asyncio
    async def test_web_create_auto_verifies_same_domain(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "web-auto-existing", url="https://webauto.example.com/a")
        svc1.domain_verified = True
        svc1.domain_challenge = "web-challenge-xyz"
        await db.commit()

        resp = await client.post("/submit", data={
            "name": "Web AutoVerify",
            "url": "https://webauto.example.com/b",
            "categories": "1",
        })
        assert resp.status_code == 200

        result = await db.execute(
            select(Service).where(Service.url == "https://webauto.example.com/b")
        )
        new_svc = result.scalars().first()
        assert new_svc is not None
        assert new_svc.domain_verified is True
        assert new_svc.domain_challenge == "web-challenge-xyz"

    @pytest.mark.asyncio
    async def test_new_service_on_unverified_domain_stays_unverified(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "unv-existing", url="https://unver.example.com/a")
        assert svc1.domain_verified is False

        resp = await client.post("/api/v1/services", json={
            "name": "Still Unverified",
            "url": "https://unver.example.com/b",
        })
        assert resp.status_code == 201
        assert resp.json()["domain_verified"] is False

    @pytest.mark.asyncio
    async def test_purged_overwrite_inherits_verification(self, db: AsyncSession):
        """Test overwrite_purged_service accepts and applies domain verification."""
        svc, _ = await _create_service(db, "purge-ow", url="https://purgeow.example.com/x")
        svc.status = "purged"
        await db.commit()

        await overwrite_purged_service(
            db, svc,
            name="Resubmitted", slug="purge-ow-new",
            domain_verified=True, domain_challenge="inherited-token",
        )
        await db.commit()
        await db.refresh(svc)

        assert svc.domain_verified is True
        assert svc.domain_challenge == "inherited-token"
        assert svc.status == "unverified"  # reset from purged


class TestChallengePersistence:
    """domain_challenge should persist after verification, not be nulled out."""

    @pytest.mark.asyncio
    async def test_api_verify_preserves_challenge(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "persist-api", url="https://persist-api.example.com")

        gen_resp = await client.post(f"/api/v1/services/{svc.slug}/recover/generate")
        challenge = gen_resp.json()["challenge"]

        with patch("app.routes.api.httpx.AsyncClient") as MockClient, \
             patch("app.routes.api.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await client.post(f"/api/v1/services/{svc.slug}/recover/verify")

        await db.refresh(svc)
        assert svc.domain_verified is True
        assert svc.domain_challenge == challenge  # preserved, not None

    @pytest.mark.asyncio
    async def test_web_verify_preserves_challenge(self, client: AsyncClient, db: AsyncSession):
        svc, _ = await _create_service(db, "persist-web", url="https://persist-web.example.com")

        await client.post(f"/services/{svc.slug}/recover", data={"action": "generate"})
        await db.refresh(svc)
        challenge = svc.domain_challenge

        with patch("app.routes.web.httpx.AsyncClient") as MockClient, \
             patch("app.routes.web.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await client.post(f"/services/{svc.slug}/recover", data={"action": "verify"})

        await db.refresh(svc)
        assert svc.domain_verified is True
        assert svc.domain_challenge == challenge  # preserved, not None

    @pytest.mark.asyncio
    async def test_challenge_copied_to_same_domain_services(self, client: AsyncClient, db: AsyncSession):
        svc1, _ = await _create_service(db, "copy-ch1", url="https://copych.example.com/a")
        svc2, _ = await _create_service(db, "copy-ch2", url="https://copych.example.com/b")

        gen_resp = await client.post(f"/api/v1/services/{svc1.slug}/recover/generate")
        challenge = gen_resp.json()["challenge"]

        with patch("app.routes.api.httpx.AsyncClient") as MockClient, \
             patch("app.routes.api.is_public_hostname", return_value=True):
            mock_resp = AsyncMock()
            mock_resp.text = challenge
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            await client.post(f"/api/v1/services/{svc1.slug}/recover/verify")

        await db.refresh(svc1)
        await db.refresh(svc2)
        assert svc1.domain_challenge == challenge
        assert svc2.domain_challenge == challenge


class TestLivenessProbeVerification:
    """Liveness probes should re-check domain verification tokens."""

    @pytest.mark.asyncio
    async def test_verification_revoked_on_mismatch(self, db: AsyncSession):
        from contextlib import asynccontextmanager
        from db.scrape_sources import recheck_existing

        svc, _ = await _create_service(db, "probe-mismatch", url="https://probemm.example.com/api")
        svc.domain_verified = True
        svc.domain_challenge = "correct-token"
        svc.status = "live"
        await db.commit()

        # Build mock HTTP client with proper response objects
        probe_resp = MagicMock(status_code=402, headers={}, text="")
        verify_resp = MagicMock(status_code=200, text="wrong-token")

        async def mock_request(method, url, **kw):
            return probe_resp

        async def mock_get(url, **kw):
            if "well-known" in url:
                return verify_resp
            return probe_resp

        mock_client = AsyncMock()
        mock_client.request = mock_request
        mock_client.get = mock_get

        @asynccontextmanager
        async def mock_session():
            yield db

        with patch("db.scrape_sources.init_db", new_callable=AsyncMock), \
             patch("db.scrape_sources.async_session", mock_session), \
             patch("db.scrape_sources.asyncio.sleep", new_callable=AsyncMock):
            stats = await recheck_existing(mock_client, dry_run=False, verbose=False)

        await db.refresh(svc)
        assert svc.domain_verified is False
        assert stats["verified_revoked"] == 1

    @pytest.mark.asyncio
    async def test_verification_kept_on_match(self, db: AsyncSession):
        from contextlib import asynccontextmanager
        from db.scrape_sources import recheck_existing

        svc, _ = await _create_service(db, "probe-match", url="https://probem.example.com/api")
        svc.domain_verified = True
        svc.domain_challenge = "correct-token"
        svc.status = "live"
        await db.commit()

        probe_resp = MagicMock(status_code=402, headers={}, text="")
        verify_resp = MagicMock(status_code=200, text="correct-token")

        async def mock_request(method, url, **kw):
            return probe_resp

        async def mock_get(url, **kw):
            if "well-known" in url:
                return verify_resp
            return probe_resp

        mock_client = AsyncMock()
        mock_client.request = mock_request
        mock_client.get = mock_get

        @asynccontextmanager
        async def mock_session():
            yield db

        with patch("db.scrape_sources.init_db", new_callable=AsyncMock), \
             patch("db.scrape_sources.async_session", mock_session), \
             patch("db.scrape_sources.asyncio.sleep", new_callable=AsyncMock):
            stats = await recheck_existing(mock_client, dry_run=False, verbose=False)

        await db.refresh(svc)
        assert svc.domain_verified is True
        assert stats["verified_revoked"] == 0

    @pytest.mark.asyncio
    async def test_verification_revoked_on_fetch_failure(self, db: AsyncSession):
        from contextlib import asynccontextmanager
        from db.scrape_sources import recheck_existing

        svc, _ = await _create_service(db, "probe-fail", url="https://probefail.example.com/api")
        svc.domain_verified = True
        svc.domain_challenge = "some-token"
        svc.status = "live"
        await db.commit()

        probe_resp = MagicMock(status_code=402, headers={}, text="")

        async def mock_request(method, url, **kw):
            return probe_resp

        async def mock_get(url, **kw):
            if "well-known" in url:
                raise ConnectionError("unreachable")
            return probe_resp

        mock_client = AsyncMock()
        mock_client.request = mock_request
        mock_client.get = mock_get

        @asynccontextmanager
        async def mock_session():
            yield db

        with patch("db.scrape_sources.init_db", new_callable=AsyncMock), \
             patch("db.scrape_sources.async_session", mock_session), \
             patch("db.scrape_sources.asyncio.sleep", new_callable=AsyncMock):
            stats = await recheck_existing(mock_client, dry_run=False, verbose=False)

        await db.refresh(svc)
        assert svc.domain_verified is False
        assert stats["verified_revoked"] == 1
