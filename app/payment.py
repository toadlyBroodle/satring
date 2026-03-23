"""Unified multi-protocol payment gate: L402 (Lightning) + x402 (USDC) + MPP (Lightning).

Routes requests to the appropriate protocol handler based on headers.
When multiple protocols are configured, a 402 challenge includes all
applicable headers so clients can pick whichever they support.

Header routing:
  - Authorization: L402/LSAT ... -> L402 path
  - Authorization: Payment ...   -> MPP path (Lightning via Payment auth scheme)
  - PAYMENT-SIGNATURE header     -> x402 path (USDC via facilitator)
  - No auth                      -> 402 with all protocol challenges
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, payments_enabled, x402_enabled
from app.l402 import require_l402, create_invoice, mint_macaroon, check_and_consume_payment
from app.mpp import require_mpp, build_mpp_challenge
from app.x402 import require_x402, build_payment_required

logger = logging.getLogger("satring.payment")


async def require_payment(
    request: Request,
    amount_sats: int,
    price_usd: str,
    memo: str,
    db: AsyncSession | None = None,
) -> dict | None:
    """Unified payment gate supporting L402, x402, and MPP protocols.

    Returns None (L402/MPP path or test mode) or a settlement dict (x402 path).
    Raises HTTPException(402) with appropriate challenge headers if unpaid.

    Pass `db` to enable replay protection via ConsumedPayment table.
    """
    # Test mode bypass
    if not payments_enabled():
        return None

    auth = request.headers.get("Authorization", "")
    has_l402 = auth.startswith("L402 ") or auth.startswith("LSAT ")
    has_mpp = auth.startswith("Payment ")
    has_x402 = bool(request.headers.get("payment-signature"))

    # L402 auth header present: delegate to L402 handler
    if has_l402:
        await require_l402(request=request, db=db, amount_sats=amount_sats, memo=memo)
        return None

    # MPP Payment auth header present: delegate to MPP handler
    if has_mpp:
        await require_mpp(request=request, db=db, amount_sats=amount_sats, memo=memo)
        return None

    # x402 payment signature present: delegate to x402 handler
    if has_x402 and x402_enabled():
        settlement = await require_x402(
            request=request,
            price_usd=price_usd,
            description=memo,
        )

        # SECURITY: Record tx hash to prevent replay of the same settlement.
        # The facilitator handles on-chain replay, but this prevents a client
        # from reusing a valid PAYMENT-SIGNATURE to hit our endpoint twice.
        tx_hash = settlement.get("transaction", "") if settlement else ""
        if not tx_hash:
            logger.error("x402 settlement succeeded but returned no transaction hash")
            raise HTTPException(status_code=502, detail="x402 settlement missing transaction hash")
        if db is not None:
            consumed = await check_and_consume_payment(tx_hash, db)
            if not consumed:
                logger.warning(f"x402 replay blocked: tx={tx_hash}")
                raise HTTPException(
                    status_code=402,
                    detail="x402 payment already consumed",
                )

        return settlement

    # No auth headers: return 402 challenge with all configured protocols
    invoice_data = await create_invoice(amount_sats, memo)

    # L402 challenge
    macaroon_b64 = mint_macaroon(invoice_data["payment_hash"])
    l402_challenge = (
        f'L402 macaroon="{macaroon_b64}", '
        f'invoice="{invoice_data["payment_request"]}"'
    )

    # MPP challenge (uses same invoice, different wire format)
    mpp_challenge = build_mpp_challenge(
        amount_sats, invoice_data["payment_hash"],
        invoice_data["payment_request"], memo,
    )

    # Combine L402 and MPP in WWW-Authenticate (comma-separated per RFC 9110)
    headers = {"WWW-Authenticate": f"{l402_challenge}, {mpp_challenge}"}

    # Add x402 challenge if configured
    if x402_enabled():
        resource_url = str(request.url)
        x402_challenge = build_payment_required(price_usd, memo, resource_url, request.method)
        headers["PAYMENT-REQUIRED"] = x402_challenge

    raise HTTPException(
        status_code=402,
        detail="Payment Required",
        headers=headers,
    )
