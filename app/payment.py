"""Unified dual-protocol payment gate: L402 (Lightning) + x402 (USDC).

Routes requests to the appropriate protocol handler based on headers.
When both protocols are configured, a 402 challenge includes both
WWW-Authenticate (L402) and PAYMENT-REQUIRED (x402) headers so
clients can pick whichever they support.
"""

import logging

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, payments_enabled, x402_enabled
from app.l402 import require_l402, create_invoice, mint_macaroon, check_and_consume_payment
from app.x402 import require_x402, build_payment_required

logger = logging.getLogger("satring.payment")


async def require_payment(
    request: Request,
    amount_sats: int,
    price_usd: str,
    memo: str,
    resource_url: str,
    db: AsyncSession | None = None,
) -> dict | None:
    """Unified payment gate supporting both L402 and x402 protocols.

    Returns None (L402 path or test mode) or a settlement dict (x402 path).
    Raises HTTPException(402) with appropriate challenge headers if unpaid.

    Pass `db` to enable x402 replay protection via ConsumedPayment table.
    """
    # Test mode bypass
    if not payments_enabled():
        return None

    auth = request.headers.get("Authorization", "")
    has_l402 = auth.startswith("L402 ") or auth.startswith("LSAT ")
    has_x402 = bool(request.headers.get("payment-signature"))

    # L402 auth header present: delegate to L402 handler
    if has_l402:
        await require_l402(request=request, amount_sats=amount_sats, memo=memo)
        return None

    # x402 payment signature present: delegate to x402 handler
    if has_x402 and x402_enabled():
        settlement = await require_x402(
            request=request,
            price_usd=price_usd,
            resource_url=resource_url,
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

    # No auth headers: return 402 challenge with both protocols
    # Build L402 challenge
    invoice_data = await create_invoice(amount_sats, memo)
    macaroon_b64 = mint_macaroon(invoice_data["payment_hash"])
    l402_challenge = (
        f'L402 macaroon="{macaroon_b64}", '
        f'invoice="{invoice_data["payment_request"]}"'
    )

    headers = {"WWW-Authenticate": l402_challenge}

    # Add x402 challenge if configured
    if x402_enabled():
        x402_challenge = build_payment_required(price_usd, resource_url, memo)
        headers["PAYMENT-REQUIRED"] = x402_challenge

    raise HTTPException(
        status_code=402,
        detail="Payment Required",
        headers=headers,
    )
