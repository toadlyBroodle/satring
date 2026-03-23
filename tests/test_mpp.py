"""Tests for app/mpp.py: MPP (Machine Payments Protocol) Lightning payment handler."""

import hashlib
import json
import time
from unittest.mock import patch, AsyncMock

import pytest
from starlette.requests import Request

from app.mpp import (
    _b64url_encode,
    _b64url_decode,
    _compute_challenge_id,
    _verify_challenge_id,
    build_mpp_challenge,
    parse_mpp_credential,
    verify_mpp_credential,
    build_receipt,
    require_mpp,
    _MPP_REALM,
    _MPP_METHOD,
    _MPP_INTENT,
)


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------

class TestBase64url:
    def test_roundtrip(self):
        data = b'{"hello": "world"}'
        encoded = _b64url_encode(data)
        assert "=" not in encoded  # no padding
        assert _b64url_decode(encoded) == data

    def test_url_safe_chars(self):
        # bytes that produce + and / in standard base64
        data = b"\xfb\xff\xfe"
        encoded = _b64url_encode(data)
        assert "+" not in encoded
        assert "/" not in encoded


# ---------------------------------------------------------------------------
# HMAC challenge binding
# ---------------------------------------------------------------------------

class TestChallengeHMAC:
    def test_compute_and_verify(self):
        expires = str(int(time.time()) + 600)
        cid = _compute_challenge_id("satring.com", "lightning", "charge", "req123", expires)
        assert isinstance(cid, str)
        assert len(cid) == 64  # SHA256 hex

        assert _verify_challenge_id(cid, "satring.com", "lightning", "charge", "req123", expires)

    def test_wrong_realm_fails(self):
        expires = str(int(time.time()) + 600)
        cid = _compute_challenge_id("satring.com", "lightning", "charge", "req123", expires)
        assert not _verify_challenge_id(cid, "other.com", "lightning", "charge", "req123", expires)

    def test_expired_challenge_fails(self):
        expires = str(int(time.time()) - 10)  # already expired
        cid = _compute_challenge_id("satring.com", "lightning", "charge", "req123", expires)
        assert not _verify_challenge_id(cid, "satring.com", "lightning", "charge", "req123", expires)

    def test_tampered_id_fails(self):
        expires = str(int(time.time()) + 600)
        cid = _compute_challenge_id("satring.com", "lightning", "charge", "req123", expires)
        tampered = "a" * 64
        assert not _verify_challenge_id(tampered, "satring.com", "lightning", "charge", "req123", expires)


# ---------------------------------------------------------------------------
# Build challenge
# ---------------------------------------------------------------------------

class TestBuildChallenge:
    def test_challenge_format(self):
        challenge = build_mpp_challenge(100, "abc123" * 10, "lnbc100n1mock", "test charge")
        assert challenge.startswith("Payment ")
        assert 'method="lightning"' in challenge
        assert 'realm="satring.com"' in challenge
        assert 'intent="charge"' in challenge
        assert 'request="' in challenge
        assert 'id="' in challenge
        assert 'expires="' in challenge
        assert 'description="test charge"' in challenge

    def test_request_contains_invoice(self):
        challenge = build_mpp_challenge(100, "hash123", "lnbc100n1mock")
        # Extract request param
        start = challenge.index('request="') + len('request="')
        end = challenge.index('"', start)
        request_b64 = challenge[start:end]
        request_json = json.loads(_b64url_decode(request_b64))

        assert request_json["amount"] == "100"
        assert request_json["currency"] == "sat"
        assert request_json["methodDetails"]["invoice"] == "lnbc100n1mock"
        assert request_json["methodDetails"]["paymentHash"] == "hash123"
        assert request_json["methodDetails"]["network"] == "mainnet"


# ---------------------------------------------------------------------------
# Parse credential
# ---------------------------------------------------------------------------

class TestParseCredential:
    def test_valid_credential(self):
        cred_obj = {
            "challenge": {"id": "abc", "realm": "satring.com"},
            "payload": {"preimage": "dead" * 16},
        }
        token = _b64url_encode(json.dumps(cred_obj).encode())
        result = parse_mpp_credential(f"Payment {token}")
        assert result == cred_obj

    def test_invalid_encoding(self):
        assert parse_mpp_credential("Payment !!!invalid!!!") is None

    def test_missing_prefix(self):
        assert parse_mpp_credential("Bearer token123") is None


# ---------------------------------------------------------------------------
# Verify credential (full round-trip)
# ---------------------------------------------------------------------------

class TestVerifyCredential:
    def _make_credential(self, preimage: bytes) -> dict:
        """Build a valid MPP credential for a given preimage."""
        payment_hash = hashlib.sha256(preimage).hexdigest()
        amount_sats = 100
        invoice = "lnbc100n1mock"

        expires = str(int(time.time()) + 600)
        request_obj = {
            "amount": str(amount_sats),
            "currency": "sat",
            "methodDetails": {
                "invoice": invoice,
                "paymentHash": payment_hash,
                "network": "mainnet",
            },
        }
        request_b64 = _b64url_encode(json.dumps(request_obj, separators=(",", ":")).encode())
        challenge_id = _compute_challenge_id(
            _MPP_REALM, _MPP_METHOD, _MPP_INTENT, request_b64, expires,
        )

        return {
            "challenge": {
                "id": challenge_id,
                "realm": _MPP_REALM,
                "method": _MPP_METHOD,
                "intent": _MPP_INTENT,
                "request": request_b64,
                "expires": expires,
            },
            "payload": {
                "preimage": preimage.hex(),
            },
        }

    def test_valid_credential_passes(self):
        preimage = b"mpp-test-preimage-bytes!"
        credential = self._make_credential(preimage)
        assert verify_mpp_credential(credential) is True

    def test_wrong_preimage_fails(self):
        preimage = b"correct-preimage-bytes!!"
        credential = self._make_credential(preimage)
        credential["payload"]["preimage"] = b"wrong-preimage-bytes!!!".hex()
        assert verify_mpp_credential(credential) is False

    def test_tampered_challenge_id_fails(self):
        preimage = b"tamper-test-preimage!!!!"
        credential = self._make_credential(preimage)
        credential["challenge"]["id"] = "f" * 64
        assert verify_mpp_credential(credential) is False

    def test_expired_challenge_fails(self):
        preimage = b"expired-test-preimage!!!"
        credential = self._make_credential(preimage)
        credential["challenge"]["expires"] = str(int(time.time()) - 10)
        # Recompute ID with expired time (otherwise HMAC fails first)
        request_b64 = credential["challenge"]["request"]
        credential["challenge"]["id"] = _compute_challenge_id(
            _MPP_REALM, _MPP_METHOD, _MPP_INTENT,
            request_b64, credential["challenge"]["expires"],
        )
        assert verify_mpp_credential(credential) is False

    def test_wrong_method_fails(self):
        preimage = b"method-test-preimage!!!!"
        credential = self._make_credential(preimage)
        credential["challenge"]["method"] = "tempo"
        assert verify_mpp_credential(credential) is False

    def test_garbage_credential_fails(self):
        assert verify_mpp_credential({}) is False
        assert verify_mpp_credential({"challenge": {}, "payload": {}}) is False


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------

class TestReceipt:
    def test_receipt_format(self):
        receipt_b64 = build_receipt("abc123")
        receipt = json.loads(_b64url_decode(receipt_b64))
        assert receipt["status"] == "settled"
        assert receipt["method"] == "lightning"
        assert receipt["reference"] == "abc123"
        assert "timestamp" in receipt


# ---------------------------------------------------------------------------
# require_mpp dependency
# ---------------------------------------------------------------------------

class TestRequireMpp:
    @pytest.mark.asyncio
    async def test_test_mode_bypass(self):
        with patch("app.mpp.payments_enabled", return_value=False):
            result = await require_mpp(request=None)
            assert result is None

    @pytest.mark.asyncio
    async def test_no_request_raises_500(self):
        from fastapi import HTTPException
        with patch("app.mpp.payments_enabled", return_value=True):
            with pytest.raises(HTTPException) as exc_info:
                await require_mpp(request=None)
            assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_no_auth_returns_402_with_payment_challenge(self):
        from fastapi import HTTPException

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/test",
            "headers": [],
        }
        request = Request(scope)

        with patch("app.mpp.payments_enabled", return_value=True), \
             patch("app.mpp.settings") as mock_settings, \
             patch("app.mpp.create_invoice", new_callable=AsyncMock) as mock_invoice:
            mock_settings.AUTH_ROOT_KEY = "real-key"
            mock_settings.AUTH_PRICE_SATS = 100
            mock_invoice.return_value = {
                "payment_hash": "abcd1234" * 8,
                "payment_request": "lnbc100n1fake",
            }

            with pytest.raises(HTTPException) as exc_info:
                await require_mpp(request=request)

            assert exc_info.value.status_code == 402
            assert "WWW-Authenticate" in exc_info.value.headers
            www_auth = exc_info.value.headers["WWW-Authenticate"]
            assert www_auth.startswith("Payment ")
            assert 'method="lightning"' in www_auth

    @pytest.mark.asyncio
    async def test_valid_mpp_token_passes(self):
        preimage = b"valid-mpp-preimage-test!"
        payment_hash = hashlib.sha256(preimage).hexdigest()

        root_key = "mpp-test-root-key"

        with patch("app.mpp.payments_enabled", return_value=True), \
             patch("app.mpp.settings") as mock_settings:
            mock_settings.AUTH_ROOT_KEY = root_key

            # Build a valid credential
            expires = str(int(time.time()) + 600)
            request_obj = {
                "amount": "100",
                "currency": "sat",
                "methodDetails": {
                    "invoice": "lnbc100n1mock",
                    "paymentHash": payment_hash,
                    "network": "mainnet",
                },
            }
            request_b64 = _b64url_encode(json.dumps(request_obj, separators=(",", ":")).encode())
            challenge_id = _compute_challenge_id(
                _MPP_REALM, _MPP_METHOD, _MPP_INTENT, request_b64, expires,
            )
            cred_obj = {
                "challenge": {
                    "id": challenge_id,
                    "realm": _MPP_REALM,
                    "method": _MPP_METHOD,
                    "intent": _MPP_INTENT,
                    "request": request_b64,
                    "expires": expires,
                },
                "payload": {"preimage": preimage.hex()},
            }
            token = _b64url_encode(json.dumps(cred_obj).encode())

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/test",
                "headers": [
                    (b"authorization", f"Payment {token}".encode()),
                ],
            }
            request = Request(scope)
            result = await require_mpp(request=request)
            assert result is not None
            assert "payment_hash" in result

    @pytest.mark.asyncio
    async def test_invalid_mpp_token_returns_402(self):
        from fastapi import HTTPException

        with patch("app.mpp.payments_enabled", return_value=True), \
             patch("app.mpp.settings") as mock_settings, \
             patch("app.mpp.create_invoice", new_callable=AsyncMock) as mock_invoice:
            mock_settings.AUTH_ROOT_KEY = "real-key"
            mock_settings.AUTH_PRICE_SATS = 100
            mock_invoice.return_value = {
                "payment_hash": "abcd1234" * 8,
                "payment_request": "lnbc100n1fake",
            }

            # Send garbage credential
            token = _b64url_encode(json.dumps({
                "challenge": {"id": "bad"},
                "payload": {"preimage": "bad"},
            }).encode())

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/test",
                "headers": [
                    (b"authorization", f"Payment {token}".encode()),
                ],
            }
            request = Request(scope)

            with pytest.raises(HTTPException) as exc_info:
                await require_mpp(request=request)
            assert exc_info.value.status_code == 402
            detail = exc_info.value.detail
            assert detail["type"] == "https://paymentauth.org/problems/verification-failed"
