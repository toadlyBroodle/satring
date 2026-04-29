"""Tests for auto-sending verification emails on service creation."""

from unittest.mock import patch, MagicMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.utils import extract_email, send_verify_email

# All send_verify_email calls need payments_enabled=True since email is
# skipped in test mode. This decorator stacks with the SMTP mock.
_enable_payments = patch("app.config.payments_enabled", return_value=True)

# send_verify_email runs an MX precheck against the recipient domain to
# block bots/typos before hitting SMTP. The integration-test fixtures below
# use intentionally fake subdomains (emailtest.example.com, embedded...,
# webmail...) with no MX records, so this fixture stubs the resolver to
# always succeed. The point of those tests is the wiring (extract → send),
# not DNS behavior.
@pytest.fixture
def _mx_ok():
    fake_resolver = MagicMock()
    fake_resolver.resolve = MagicMock(return_value=[MagicMock()])
    with patch("dns.resolver.Resolver", return_value=fake_resolver):
        yield


# ---------------------------------------------------------------------------
# Unit: extract_email
# ---------------------------------------------------------------------------

class TestExtractEmail:
    def test_plain_email(self):
        assert extract_email("alice@example.com") == "alice@example.com"

    def test_email_in_text(self):
        assert extract_email("Contact: alice@example.com for info") == "alice@example.com"

    def test_email_with_name(self):
        assert extract_email("Alice <alice@example.com>") == "alice@example.com"

    def test_email_among_urls(self):
        assert extract_email("https://example.com, bob@test.org") == "bob@test.org"

    def test_no_email(self):
        assert extract_email("@twitter_handle") is None

    def test_empty_string(self):
        assert extract_email("") is None

    def test_url_only(self):
        assert extract_email("https://example.com") is None

    def test_plus_addressing(self):
        assert extract_email("user+tag@example.com") == "user+tag@example.com"


# ---------------------------------------------------------------------------
# Unit: send_verify_email (mock SMTP)
# ---------------------------------------------------------------------------

class TestSendVerifyEmail:
    @_enable_payments
    @patch("app.utils.smtplib.SMTP")
    def test_sends_with_correct_fields(self, mock_smtp_cls, _):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_verify_email("alice@example.com", "my-service", "example.com")

        mock_srv.send_message.assert_called_once()
        msg = mock_srv.send_message.call_args[0][0]
        assert msg["To"] == "alice@example.com"
        assert msg["Subject"] == "Verify your service on satring.com"
        assert "my-service" in msg.get_payload()
        assert "example.com/.well-known/satring-verify" in msg.get_payload()

    @_enable_payments
    @patch("app.utils.smtplib.SMTP")
    def test_substitutes_placeholders(self, mock_smtp_cls, _):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        send_verify_email("bob@test.org", "cool-api", "api.cool.dev")

        msg = mock_srv.send_message.call_args[0][0]
        body = msg.get_payload()
        assert "cool-api" in body
        assert "api.cool.dev" in body
        assert "SERVICE_SLUG" not in body
        assert "YOUR_DOMAIN" not in body

    @_enable_payments
    @patch("app.utils.smtplib.SMTP", side_effect=ConnectionRefusedError)
    def test_smtp_failure_does_not_raise(self, mock_smtp_cls, _):
        # Should log error but not propagate
        send_verify_email("alice@example.com", "my-service", "example.com")

    @patch("app.utils.smtplib.SMTP")
    def test_skipped_in_test_mode(self, mock_smtp_cls):
        """Email should not be sent when payments are disabled (test mode)."""
        send_verify_email("alice@example.com", "my-service", "example.com")
        mock_smtp_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: API service creation triggers email
# ---------------------------------------------------------------------------

class TestAPICreateServiceEmail:
    @pytest.mark.asyncio
    @_enable_payments
    @patch("app.utils.smtplib.SMTP")
    async def test_email_sent_when_contact_has_email(self, mock_smtp_cls, _, _mx_ok, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/api/v1/services", json={
            "name": "Email Test API",
            "url": "https://emailtest.example.com",
            "owner_contact": "owner@emailtest.example.com",
        })
        assert resp.status_code == 201

        mock_srv.send_message.assert_called_once()
        msg = mock_srv.send_message.call_args[0][0]
        assert msg["To"] == "owner@emailtest.example.com"
        assert "emailtest.example.com" in msg.get_payload()

    @pytest.mark.asyncio
    @_enable_payments
    @patch("app.utils.smtplib.SMTP")
    async def test_email_sent_when_contact_contains_email(self, mock_smtp_cls, _, _mx_ok, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/api/v1/services", json={
            "name": "Embedded Email API",
            "url": "https://embedded.example.com",
            "owner_contact": "Twitter: @foo, email: dev@embedded.example.com",
        })
        assert resp.status_code == 201

        mock_srv.send_message.assert_called_once()
        msg = mock_srv.send_message.call_args[0][0]
        assert msg["To"] == "dev@embedded.example.com"

    @pytest.mark.asyncio
    @patch("app.utils.smtplib.SMTP")
    async def test_no_email_when_contact_has_no_email(self, mock_smtp_cls, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/api/v1/services", json={
            "name": "No Email API",
            "url": "https://noemail.example.com",
            "owner_contact": "@twitter_handle",
        })
        assert resp.status_code == 201
        mock_srv.send_message.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.utils.smtplib.SMTP")
    async def test_no_email_when_contact_empty(self, mock_smtp_cls, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/api/v1/services", json={
            "name": "Empty Contact API",
            "url": "https://emptycontact.example.com",
        })
        assert resp.status_code == 201
        mock_srv.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: Web form submission triggers email
# ---------------------------------------------------------------------------

class TestWebSubmitServiceEmail:
    @pytest.mark.asyncio
    @_enable_payments
    @patch("app.utils.smtplib.SMTP")
    async def test_web_submit_sends_email(self, mock_smtp_cls, _, _mx_ok, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/submit", content=(
            "name=Web+Email+Test"
            "&url=https%3A%2F%2Fwebmail.example.com"
            "&owner_contact=admin%40webmail.example.com"
            "&categories=9"
        ), headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert resp.status_code == 200

        mock_srv.send_message.assert_called_once()
        msg = mock_srv.send_message.call_args[0][0]
        assert msg["To"] == "admin@webmail.example.com"

    @pytest.mark.asyncio
    @patch("app.utils.smtplib.SMTP")
    async def test_web_submit_no_email_without_contact(self, mock_smtp_cls, client: AsyncClient):
        mock_srv = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_srv)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        resp = await client.post("/submit", content=(
            "name=Web+No+Email+Test"
            "&url=https%3A%2F%2Fwebnomail.example.com"
            "&categories=9"
        ), headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert resp.status_code == 200
        mock_srv.send_message.assert_not_called()
