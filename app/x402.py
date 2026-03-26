"""x402 protocol implementation: build challenges, parse signatures, facilitator calls.

Implements the x402 v2 protocol directly (no heavy web3/ethers deps).
The protocol is JSON + base64 + HTTP calls to the facilitator (xpay.sh).
"""

import base64
import json
import logging

import httpx
from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger("satring.x402")


def _usd_to_usdc_units(price_usd: str) -> str:
    """Convert a USD price string (e.g. '0.50') to USDC base units (6 decimals).

    '0.50' -> '500000', '0.01' -> '10000', '2.50' -> '2500000'
    """
    from decimal import Decimal
    return str(int(Decimal(price_usd) * 10**6))


def _build_requirements_object(price_usd: str) -> dict:
    """Build a single PaymentRequirements dict per x402 v2 spec.

    In v2, requirements contain only payment parameters (no resource/description).
    Amount is in USDC base units (6 decimals).
    """
    return {
        "scheme": "exact",
        "network": settings.X402_NETWORK,
        "asset": settings.X402_ASSET,
        "amount": _usd_to_usdc_units(price_usd),
        "payTo": settings.X402_PAY_TO,
        "maxTimeoutSeconds": 300,
        "extra": {
            "name": "USD Coin",
            "version": "2",
        },
    }


def _build_bazaar_info(method: str) -> dict:
    """Build bazaar extension info block. GET uses queryParams, POST uses body."""
    if method in ("POST", "PUT", "PATCH"):
        return {
            "input": {
                "type": "http",
                "method": method,
                "bodyType": "json",
                "body": {},
            },
            "output": {"type": "json"},
        }
    return {
        "input": {
            "type": "http",
            "method": method,
            "queryParams": {},
        },
        "output": {"type": "json"},
    }


def _build_bazaar_schema(method: str) -> dict:
    """Build JSON Schema for bazaar info. Validator requires body or queryParams."""
    if method in ("POST", "PUT", "PATCH"):
        input_props = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string"},
            "bodyType": {"type": "string"},
            "body": {"type": "object"},
        }
    else:
        input_props = {
            "type": {"type": "string", "const": "http"},
            "method": {"type": "string"},
            "queryParams": {"type": "object"},
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["input", "output"],
        "properties": {
            "input": {
                "type": "object",
                "required": ["type", "method"],
                "properties": input_props,
            },
            "output": {
                "type": "object",
                "required": ["type"],
                "properties": {
                    "type": {"type": "string", "const": "json"},
                    "example": {"type": "object"},
                },
            },
        },
    }


def build_payment_required(
    price_usd: str,
    description: str,
    resource_url: str = "https://satring.com/api",
    method: str = "GET",
) -> str:
    """Build base64-encoded PaymentRequired JSON per x402 v2 spec.

    Returns the value for the PAYMENT-REQUIRED response header.
    In v2, resource is a ResourceInfo object (not a plain string).
    Includes Coinbase Bazaar extensions for x402scan discovery.
    """
    payload = {
        "x402Version": 2,
        "resource": {
            "url": resource_url,
            "description": description,
            "mimeType": "application/json",
        },
        "accepts": [_build_requirements_object(price_usd)],
        "extensions": {
            "bazaar": {
                "info": _build_bazaar_info(method),
                "schema": _build_bazaar_schema(method),
            },
        },
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def parse_payment_signature(header_value: str) -> dict | None:
    """Base64-decode and JSON-parse the PAYMENT-SIGNATURE header.

    Returns the parsed dict, or None on any failure.
    """
    try:
        decoded = base64.b64decode(header_value)
        return json.loads(decoded)
    except Exception:
        return None


async def verify_and_settle_x402(
    payment_payload: dict,
    requirements_object: dict,
) -> dict:
    """POST to facilitator /verify, then /settle. Return settlement response.

    Args:
        payment_payload: Decoded JSON from the PAYMENT-SIGNATURE header.
        requirements_object: Decoded PaymentRequirements dict (not base64).

    Raises HTTPException on failure.
    """
    facilitator = settings.X402_FACILITATOR_URL.rstrip("/")
    timeout = httpx.Timeout(30.0)

    request_body = {
        "x402Version": 2,
        "paymentPayload": payment_payload,
        "paymentRequirements": requirements_object,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Verify
        try:
            verify_resp = await client.post(
                f"{facilitator}/verify",
                json=request_body,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            raise HTTPException(status_code=502, detail="x402 facilitator unreachable")

        if verify_resp.status_code != 200:
            detail = verify_resp.text[:200] if verify_resp.text else "Verification failed"
            logger.warning(f"x402 verify failed: {detail}")
            raise HTTPException(status_code=402, detail=f"x402 payment verification failed: {detail}")

        try:
            verify_data = verify_resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail="x402 facilitator returned invalid JSON on verify")

        if not verify_data.get("isValid"):
            reason = verify_data.get("invalidReason", "unknown")
            logger.warning(f"x402 verify rejected: {reason}")
            raise HTTPException(status_code=402, detail=f"x402 payment invalid: {reason}")

        # Settle
        try:
            settle_resp = await client.post(
                f"{facilitator}/settle",
                json=request_body,
            )
        except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError):
            logger.error("x402 facilitator unreachable during settlement (verify succeeded)")
            raise HTTPException(status_code=502, detail="x402 facilitator unreachable during settlement")

        if settle_resp.status_code != 200:
            detail = settle_resp.text[:200] if settle_resp.text else "Settlement failed"
            logger.error(f"x402 settle HTTP error: {detail}")
            raise HTTPException(status_code=402, detail=f"x402 settlement failed: {detail}")

        try:
            settle_data = settle_resp.json()
        except Exception:
            raise HTTPException(status_code=502, detail="x402 facilitator returned invalid JSON on settle")

        if not settle_data.get("success"):
            reason = settle_data.get("errorReason", "unknown")
            logger.error(f"x402 settle rejected: {reason} | full response: {settle_data}")
            raise HTTPException(status_code=402, detail=f"x402 settlement rejected: {reason}")

        tx_hash = settle_data.get("transaction", "")
        payer = settle_data.get("payer", "")
        logger.info(f"x402 settlement OK: tx={tx_hash} payer={payer}")

        return settle_data


async def require_x402(
    request: Request,
    price_usd: str,
    description: str,
) -> dict | None:
    """Check for PAYMENT-SIGNATURE header. If absent, raise 402 with PAYMENT-REQUIRED.

    If present, verify and settle via facilitator. Return settlement dict on success.
    """
    sig_header = request.headers.get("payment-signature")

    if not sig_header:
        # No payment: issue x402 challenge
        resource_url = str(request.url)
        payment_required = build_payment_required(price_usd, description, resource_url, request.method)
        raise HTTPException(
            status_code=402,
            detail="Payment Required (x402)",
            headers={"PAYMENT-REQUIRED": payment_required},
        )

    # Parse and verify the payment signature
    payload = parse_payment_signature(sig_header)
    if not payload:
        raise HTTPException(status_code=400, detail="Invalid PAYMENT-SIGNATURE header encoding")

    requirements = _build_requirements_object(price_usd)
    result = await verify_and_settle_x402(payload, requirements)
    return result
