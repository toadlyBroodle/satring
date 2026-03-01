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

FastAPI application serving an L402 (Lightning-paywalled) service directory. Server-rendered HTML via Jinja2 + HTMX, with a parallel JSON API.

**Route split:**
- `app/routes/api.py` (prefix `/api/v1`): JSON API with L402 payment gates via `require_l402()` dependency
- `app/routes/web.py`: HTML pages with form handling. Payment flows use invoice widgets polled via JS
- Shared data builders (`build_analytics_data`, `build_reputation_data`) live in `api.py` and are imported by `web.py`

**L402 payment flow (API):**
1. Client hits gated endpoint without auth -> 402 with `WWW-Authenticate: L402 macaroon="...", invoice="..."`
2. Client pays Lightning invoice, gets preimage
3. Client retries with `Authorization: L402 <macaroon>:<preimage>`
4. Server verifies: SHA256(preimage) == payment_hash in macaroon caveat, then checks macaroon signature

**L402 payment flow (Web):**
1. Form POST or JS fetch triggers invoice creation -> payment widget HTML returned
2. Widget shows QR + BOLT11, polls `/payment-status/{hash}` every 5s
3. On paid: resubmits form with `?payment_hash=` or fetches result HTML
4. `check_and_consume_payment()` prevents double-spend via `ConsumedPayment` table

**Test mode:** `AUTH_ROOT_KEY=test-mode` in `.env` bypasses all payment gates. The `payments_enabled()` function in `config.py` controls this.

## Key Files

- `app/config.py`: All settings, rate limits, input length constants. Change limits here, not in handlers.
- `app/l402.py`: Macaroon minting/verification, invoice creation via LNbits API
- `app/models.py`: SQLAlchemy async models (Service, Category, Rating, ConsumedPayment)
- `app/utils.py`: Slugification, edit tokens, domain extraction, SSRF protection (`is_public_hostname`)
- `app/main.py`: App factory, security middleware (CSP, CSRF origin check), category seeding

## Database

SQLite via aiosqlite async driver. Default path: `db/sr.db`. Models use SQLAlchemy async with `AsyncSession` dependency injection via `get_db()`.

Service ratings are denormalized: `avg_rating` and `rating_count` on the Service model are updated on each new Rating insert.

## Testing

Tests use async fixtures from `tests/conftest.py`:
- `client`: AsyncClient with fresh in-memory SQLite DB (function-scoped)
- `sample_service`: Pre-created service with categories
- `sample_service_with_ratings`: Service with 3 ratings
- `class_client`/`class_db`: Class-scoped variants for shared state across test methods

All tests run with `AUTH_ROOT_KEY=test-mode` (payment gates disabled).

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
- Service statuses: `unverified`, `confirmed`, `live`, `dead`, `purged` (purged = soft-deleted, excluded from all queries)
