"""Unit tests for app/x402.py: x402 protocol implementation."""

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.x402 import build_payment_required, parse_payment_signature, verify_and_settle_x402


class TestBuildPaymentRequired:
    def test_returns_base64_json(self):
        result = build_payment_required("0.50", "https://satring.com/api/v1/services", "test")
        decoded = json.loads(base64.b64decode(result))
        assert decoded["x402Version"] == 2
        assert len(decoded["accepts"]) == 1

    def test_payload_fields(self):
        result = build_payment_required("1.00", "https://example.com/resource", "Premium access")
        decoded = json.loads(base64.b64decode(result))
        accept = decoded["accepts"][0]
        assert accept["scheme"] == "exact"
        assert accept["maxAmountRequired"] == "1.00"
        assert accept["resource"] == "https://example.com/resource"
        assert accept["description"] == "Premium access"
        assert accept["maxTimeoutSeconds"] == 300


class TestParsePaymentSignature:
    def test_valid_payload(self):
        payload = {"txHash": "0xabc123", "amount": "1.00"}
        encoded = base64.b64encode(json.dumps(payload).encode()).decode()
        result = parse_payment_signature(encoded)
        assert result == payload

    def test_invalid_base64(self):
        assert parse_payment_signature("not-valid-base64!!!") is None

    def test_invalid_json(self):
        encoded = base64.b64encode(b"not json").decode()
        assert parse_payment_signature(encoded) is None

    def test_empty_string(self):
        assert parse_payment_signature("") is None


class TestVerifyAndSettleX402:
    @pytest.mark.anyio
    async def test_successful_verify_and_settle(self):
        mock_verify = MagicMock()
        mock_verify.status_code = 200
        mock_verify.json = MagicMock(return_value={"isValid": True})

        mock_settle = MagicMock()
        mock_settle.status_code = 200
        mock_settle.json = MagicMock(return_value={"txHash": "0xabc", "success": True})

        with patch("app.x402.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = [mock_verify, mock_settle]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await verify_and_settle_x402(
                {"txHash": "0xabc"},
                {"scheme": "exact", "maxAmountRequired": "0.50"},
            )
            assert result["txHash"] == "0xabc"

    @pytest.mark.anyio
    async def test_verify_failure(self):
        mock_verify = MagicMock()
        mock_verify.status_code = 400
        mock_verify.text = "Invalid payment"

        with patch("app.x402.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.return_value = mock_verify
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await verify_and_settle_x402({"txHash": "0xabc"}, {"scheme": "exact"})
            assert exc_info.value.status_code == 402

    @pytest.mark.anyio
    async def test_settle_failure(self):
        mock_verify = MagicMock()
        mock_verify.status_code = 200
        mock_verify.json = MagicMock(return_value={"isValid": True})

        mock_settle = MagicMock()
        mock_settle.status_code = 500
        mock_settle.text = "Internal error"

        with patch("app.x402.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post.side_effect = [mock_verify, mock_settle]
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await verify_and_settle_x402({"txHash": "0xabc"}, {"scheme": "exact"})
            assert exc_info.value.status_code == 402
