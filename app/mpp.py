"""MPP (Machine Payments Protocol) Lightning payment handler.

Implements the 'Payment' HTTP Authentication Scheme (draft-httpauth-payment-00)
with the Lightning charge method (draft-lightning-charge-00).

Challenge flow:
  1. Server returns 402 with WWW-Authenticate: Payment header containing a
     BOLT11 invoice in the `request` auth-param.
  2. Client pays the Lightning invoice, obtains the preimage.
  3. Client retries with Authorization: Payment <base64url-json> containing
     the preimage in payload.preimage.
  4. Server verifies the HMAC-bound challenge ID, then checks
     SHA256(preimage) == paymentHash.

Uses the same LNbits wallet as L402 for invoice creation.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from fastapi import HTTPException, Request

from app.config import settings, payments_enabled
from app.l402 import create_invoice, check_and_consume_payment

logger = logging.getLogger("satring.mpp")

# ---------------------------------------------------------------------------
# Helpers: base64url (no padding, URL-safe)
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


# ---------------------------------------------------------------------------
# HMAC challenge binding (stateless verification)
# ---------------------------------------------------------------------------

_MPP_REALM = "satring.com"
_MPP_METHOD = "lightning"
_MPP_INTENT = "charge"
_CHALLENGE_TTL = 600  # 10 minutes


def _get_mpp_secret() -> str:
    """Derive MPP HMAC secret from AUTH_ROOT_KEY.

    Uses a distinct prefix so the same root key produces different MACs
    for L402 macaroons vs MPP challenges.
    """
    return f"mpp:{settings.AUTH_ROOT_KEY}"


def _compute_challenge_id(
    realm: str,
    method: str,
    intent: str,
    request_b64: str,
    expires: str,
) -> str:
    """HMAC-SHA256 over pipe-delimited challenge fields (per spec)."""
    message = f"{realm}|{method}|{intent}|{request_b64}|{expires}"
    mac = hmac.new(
        _get_mpp_secret().encode(),
        message.encode(),
        hashlib.sha256,
    )
    return mac.hexdigest()


def _verify_challenge_id(
    challenge_id: str,
    realm: str,
    method: str,
    intent: str,
    request_b64: str,
    expires: str,
) -> bool:
    """Verify the HMAC and check expiry."""
    expected = _compute_challenge_id(realm, method, intent, request_b64, expires)
    if not hmac.compare_digest(challenge_id, expected):
        return False
    try:
        if float(expires) < time.time():
            return False
    except (ValueError, TypeError):
        return False
    return True


# ---------------------------------------------------------------------------
# Build MPP challenge (402 response)
# ---------------------------------------------------------------------------

def build_mpp_challenge(
    amount_sats: int,
    payment_hash: str,
    invoice: str,
    description: str = "",
) -> str:
    """Build the WWW-Authenticate: Payment header value.

    Returns the full header value with auth-params per draft-httpauth-payment-00.
    """
    expires = str(int(time.time()) + _CHALLENGE_TTL)

    # request auth-param: base64url-encoded JSON per draft-lightning-charge-00
    request_obj = {
        "amount": str(amount_sats),
        "currency": "sat",
        "recipient": _MPP_REALM,
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

    # Format: Payment id="...", realm="...", method="...", intent="...", request="...", expires="...", description="..."
    parts = [
        f'id="{challenge_id}"',
        f'realm="{_MPP_REALM}"',
        f'method="{_MPP_METHOD}"',
        f'intent="{_MPP_INTENT}"',
        f'request="{request_b64}"',
        f'expires="{expires}"',
    ]
    if description:
        safe_desc = description.replace('"', '\\"')
        parts.append(f'description="{safe_desc}"')

    return "Payment " + ", ".join(parts)


# ---------------------------------------------------------------------------
# Parse MPP credential (Authorization header)
# ---------------------------------------------------------------------------

def parse_mpp_credential(auth_value: str) -> dict | None:
    """Parse Authorization: Payment <base64url-json> into a dict.

    Expected structure:
    {
        "challenge": {"id": "...", "realm": "...", "method": "...", "intent": "...",
                       "request": "...", "expires": "..."},
        "payload": {"preimage": "<64-char hex>"},
        "source": "..."  (optional)
    }
    """
    try:
        token = auth_value.split(" ", 1)[1]
        decoded = _b64url_decode(token)
        return json.loads(decoded)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Verify MPP Lightning credential
# ---------------------------------------------------------------------------

def verify_mpp_credential(credential: dict) -> bool:
    """Verify an MPP Lightning credential.

    1. Check HMAC challenge binding (proves we issued this challenge).
    2. Check challenge has not expired.
    3. Verify SHA256(preimage) == paymentHash from the echoed request.
    """
    try:
        challenge = credential.get("challenge", {})
        payload = credential.get("payload", {})

        challenge_id = challenge.get("id", "")
        realm = challenge.get("realm", "")
        method = challenge.get("method", "")
        intent = challenge.get("intent", "")
        request_b64 = challenge.get("request", "")
        expires = challenge.get("expires", "")
        preimage_hex = payload.get("preimage", "")

        # Verify HMAC + expiry
        if not _verify_challenge_id(challenge_id, realm, method, intent, request_b64, expires):
            return False

        # Verify method is lightning
        if method != "lightning":
            return False

        # Decode the request to extract paymentHash
        request_json = json.loads(_b64url_decode(request_b64))
        payment_hash = request_json.get("methodDetails", {}).get("paymentHash", "")
        if not payment_hash:
            return False

        # Verify preimage: SHA256(preimage) must equal paymentHash
        preimage_bytes = bytes.fromhex(preimage_hex)
        computed_hash = hashlib.sha256(preimage_bytes).hexdigest()
        return hmac.compare_digest(computed_hash, payment_hash.lower())

    except Exception:
        return False


def _extract_payment_hash_from_credential(credential: dict) -> str | None:
    """Extract the paymentHash from an MPP credential's echoed challenge."""
    try:
        request_b64 = credential.get("challenge", {}).get("request", "")
        request_json = json.loads(_b64url_decode(request_b64))
        return request_json.get("methodDetails", {}).get("paymentHash", "")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Build Payment-Receipt header
# ---------------------------------------------------------------------------

def build_receipt(payment_hash: str) -> str:
    """Build base64url-encoded Payment-Receipt JSON.

    Per spec, receipt includes status, method, timestamp, and reference
    (paymentHash, never the preimage).
    """
    receipt = {
        "status": "settled",
        "method": _MPP_METHOD,
        "timestamp": int(time.time()),
        "reference": payment_hash,
    }
    return _b64url_encode(json.dumps(receipt, separators=(",", ":")).encode())


# ---------------------------------------------------------------------------
# require_mpp dependency
# ---------------------------------------------------------------------------

async def require_mpp(
    request: Request = None,
    db=None,
    amount_sats: int | None = None,
    memo: str | None = None,
):
    """MPP payment gate. Uses the Payment HTTP auth scheme.

    Returns a dict with receipt info on success (or None in test mode).
    Raises HTTPException(402) with Cache-Control: no-store if unpaid or invalid.
    402 error bodies use RFC 9457 Problem Details format.
    """
    if not payments_enabled():
        return None

    if request is None:
        raise HTTPException(status_code=500, detail="MPP requires request context")

    auth = request.headers.get("Authorization", "")
    price = amount_sats if amount_sats is not None else settings.AUTH_PRICE_SATS
    inv_memo = memo or "satring.com premium API access"

    if auth.startswith("Payment "):
        credential = parse_mpp_credential(auth)
        if not credential:
            await _raise_402_problem(
                "malformed-credential", "Malformed Credential",
                "Invalid base64url or JSON encoding in Payment credential.",
                price, inv_memo,
            )

        if verify_mpp_credential(credential):
            payment_hash = _extract_payment_hash_from_credential(credential)
            # SECURITY: Replay protection via ConsumedPayment table
            if db is not None and payment_hash:
                consumed = await check_and_consume_payment(payment_hash, db)
                if not consumed:
                    logger.warning(f"MPP replay blocked: payment_hash={payment_hash}")
                    await _raise_402_problem(
                        "invalid-challenge", "Challenge Already Used",
                        "This payment credential has already been consumed.",
                        price, inv_memo,
                    )
            # Return receipt info so the caller can attach Payment-Receipt header
            return {"payment_hash": payment_hash or ""}

        # Invalid credential: return fresh challenge
        await _raise_402_problem(
            "verification-failed", "Payment Verification Failed",
            "Invalid payment proof. Ensure the preimage matches the invoice.",
            price, inv_memo,
        )

    # No Payment auth: issue 402 challenge
    await _raise_402_problem(
        "payment-required", "Payment Required",
        "This resource requires payment.",
        price, inv_memo,
    )


async def _raise_402_problem(
    problem_code: str,
    title: str,
    detail: str,
    amount_sats: int,
    memo: str,
):
    """Raise a 402 with RFC 9457 Problem Details body and Cache-Control: no-store."""
    invoice_data = await create_invoice(amount_sats, memo)
    challenge = build_mpp_challenge(
        amount_sats, invoice_data["payment_hash"],
        invoice_data["payment_request"], memo,
    )
    raise HTTPException(
        status_code=402,
        detail={
            "type": f"https://paymentauth.org/problems/{problem_code}",
            "title": title,
            "status": 402,
            "detail": detail,
        },
        headers={
            "WWW-Authenticate": challenge,
            "Cache-Control": "no-store",
        },
    )
