# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run dev server
uvicorn app.main:app --reload

# Run all tests
python -m pytest

# Run single test file
python -m pytest tests/test_api.py

# Run single test by name
python -m pytest -k "test_create_service"


```

## Architecture

FastAPI application serving an L402 + x402 + MPP tri-protocol paid API directory. Server-rendered HTML via Jinja2 + HTMX, with a parallel JSON API.

**Route split:**
- `app/routes/api.py` (prefix `/api/v1`): JSON API with dual-protocol payment gates via `require_payment()` dependency
- `app/routes/web.py`: HTML pages with form handling. Payment flows use invoice widgets polled via JS
- Shared data builders (`build_analytics_data`, `build_reputation_data`) live in `api.py` and are imported by `web.py`

**Dual-protocol payment flow (API):**
- `app/payment.py` routes requests based on headers:
  1. `Authorization: L402/LSAT ...` header -> L402 path via `app/l402.py`
  2. `PAYMENT-SIGNATURE` header -> x402 path via `app/x402.py`
  3. No auth headers -> 402 with BOTH `WWW-Authenticate` (L402) and `PAYMENT-REQUIRED` (x402) headers
  4. If only L402 is configured (no X402_PAY_TO), falls back to L402-only challenge.

**L402 payment flow (API):**
1. Client hits gated endpoint without auth -> 402 with `WWW-Authenticate: L402 macaroon="...", invoice="..."`
2. Client pays Lightning invoice, gets preimage
3. Client retries with `Authorization: L402 <macaroon>:<preimage>`
4. Server verifies: SHA256(preimage) == payment_hash in macaroon caveat, then checks macaroon signature

**x402 payment flow (API):**
1. Client hits gated endpoint without auth -> 402 with `PAYMENT-REQUIRED` header (base64 JSON per x402 v2 spec)
2. Client pays via USDC on Base, gets payment signature
3. Client retries with `PAYMENT-SIGNATURE` header (base64 JSON)
4. Server verifies and settles via facilitator (xpay.sh): POST /verify, then POST /settle

**L402 payment flow (Web):**
1. Form POST or JS fetch triggers invoice creation -> payment widget HTML returned
2. Widget shows QR + BOLT11, polls `/payment-status/{hash}` every 5s
3. On paid: resubmits form with `?payment_hash=` or fetches result HTML
4. `check_and_consume_payment()` prevents double-spend via `ConsumedPayment` table

**Test mode:** `AUTH_ROOT_KEY=test-mode` in `.env` bypasses all payment gates. The `payments_enabled()` function in `config.py` controls this.

**Health monitoring:** `app/health.py` runs a background task that probes all services on an interval (default 6h). Detects L402, x402, and MPP protocols via response headers. Updates service status to `live`, `confirmed`, or `down`.

**MPP protocol (listing only):** MPP (Machine Payments Protocol by Stripe/Tempo) services can be listed and detected. MPP uses `WWW-Authenticate: Payment` headers. MPP-specific fields: `mpp_method` (tempo/stripe/lightning/custom), `mpp_realm`, `mpp_currency`. The scraper indexes mpp.dev registry as source 8. Accepting MPP payments is not yet implemented.

**Free API quota:** Free list/search endpoints return up to 10 results per IP per day (`FREE_API_RESULTS_PER_DAY` in config.py). Enforced in `paginated_services()` via in-memory daily counter. Skipped in test mode.

**Stats landing page:** `/` serves a free public stats page with protocol breakdown, category coverage, health status, growth, and leaderboards. The service directory is at `/directory`.


## Key Files

- `app/config.py`: All settings, rate limits, input length constants, x402/health probe config. Change limits here, not in handlers.
- `app/payment.py`: Unified dual-protocol payment gate (require_payment)
- `app/l402.py`: Macaroon minting/verification, invoice creation via LNbits API
- `app/x402.py`: x402 protocol: build challenges, parse signatures, facilitator calls
- `app/health.py`: Background service liveness probing
- `app/models.py`: SQLAlchemy async models (Service with x402 + MPP columns, Category, Rating, ConsumedPayment)
- `app/utils.py`: Slugification, edit tokens, domain extraction, SSRF protection (`is_public_hostname`), protocol validation (`is_valid_protocol`, `canonical_protocol`, `BASE_PROTOCOLS`)
- `app/main.py`: App factory, security middleware (CSP, CSRF origin check), category seeding, health monitor lifecycle

## Config Variables

x402 settings (env vars):
- `X402_FACILITATOR_URL`: facilitator endpoint (default: `https://facilitator.xpay.sh`)
- `X402_PAY_TO`: USDC wallet address (empty = x402 disabled)
- `X402_NETWORK`: chain ID (default: `eip155:8453` for Base mainnet)
- `X402_ASSET`: USDC contract address on Base

USD prices (parallel to sat prices):
- `AUTH_SUBMIT_PRICE_USD`, `AUTH_REVIEW_PRICE_USD`, `AUTH_BULK_PRICE_USD`, `AUTH_ANALYTICS_PRICE_USD`, `AUTH_REPUTATION_PRICE_USD`

Health probe settings:
- `HEALTH_PROBE_INTERVAL`: seconds between probe cycles (default: 21600 = 6h)
- `HEALTH_PROBE_TIMEOUT`: per-service timeout in seconds (default: 15)
- `HEALTH_PROBE_CONCURRENCY`: max concurrent probes (default: 10)

## Database

SQLite via aiosqlite async driver. Default path: `db/sr.db`. Models use SQLAlchemy async with `AsyncSession` dependency injection via `get_db()`.

Service ratings are denormalized: `avg_rating` and `rating_count` on the Service model are updated on each new Rating insert.

x402 columns (`x402_network`, `x402_asset`, `x402_pay_to`, `pricing_usd`) and MPP columns (`mpp_method`, `mpp_realm`, `mpp_currency`) are nullable on the Service model. Migration in `init_db()` adds them via `ALTER TABLE` if missing, and renames status `'dead'` to `'down'` in services and probe_history tables.

## Testing

Tests use async fixtures from `tests/conftest.py`:
- `client`: AsyncClient with fresh in-memory SQLite DB (function-scoped)
- `sample_service`: Pre-created L402 service with categories
- `sample_x402_service`: Pre-created x402 service with full metadata
- `sample_mpp_service`: Pre-created MPP service with method/realm/currency
- `sample_service_with_ratings`: Service with 3 ratings
- `class_client`/`class_db`: Class-scoped variants for shared state across test methods

All tests run with `AUTH_ROOT_KEY=test-mode` (payment gates disabled).

When testing L402 challenge flow with payments enabled, patch `app.payment.create_invoice` for API endpoint tests, or `app.l402.create_invoice` for direct `require_l402()` unit tests.

## Security Patterns

Comments prefixed `SECURITY:` mark security-critical code. Key patterns:
- Input length limits: constants in `config.py`, enforced server-side (HTML maxlength is client-only)
- URL scheme validation: reject non-http(s) to prevent stored XSS via javascript:/data: URIs
- LIKE injection: `escape_like()` in utils.py escapes `%_\` before any `.ilike()` call
- SSRF: `is_public_hostname()` blocks private/loopback IPs before any server-side HTTP fetch
- Double-spend: `ConsumedPayment` table with unique payment_hash constraint

## Conventions

- Terminal dark theme: green-on-black UI using Tailwind classes + CSS variables from `app/static/css/theme.css`
- Use CSS variables (`--text`, `--bg`, `--accent`, etc.) from theme.css for any JS-generated colors
- Pydantic response models are defined inline in `api.py` alongside the routes that use them
- Service statuses: `unverified`, `confirmed`, `live`, `down`, `purged` (purged = soft-deleted, excluded from all queries)
