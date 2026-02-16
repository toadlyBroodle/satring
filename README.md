```
 ___  __ _| |_ _ __(_)_ __   __ _
/ __|/ _` | __| '__| | '_ \ / _` |
\__ \ (_| | |_| |  | | | | | (_| |
|___/\__,_|\__|_|  |_|_| |_|\__, |
                             |___/  ⚡ L402
```

<img src="app/static/img/satring-logo-trans-bg.png" alt="satring" width="20"> [satring.com](https://satring.com) — the L402 service directory. Find, rate, and connect to Lightning-paywalled APIs.

Satring helps AI agents and developers discover L402 services — APIs that accept Bitcoin Lightning payments via the [L402 protocol](https://www.l402.org/). Browse the directory, submit your service, and let agents find you.

## Why

AI agents can now [pay for APIs autonomously](https://lightning.engineering/posts/2026-02-11-ln-agent-tools/) using Lightning. But there's no way to discover what's available. Satring is the missing directory.

## Features

- Browse, search, and filter L402-enabled APIs by category
- Submit services with Lightning payment gate (anti-spam)
- Ratings and reputation system (also Lightning-gated)
- Edit your listing with secure edit tokens
- Recover lost edit tokens via domain verification (`.well-known/satring-verify`)
- Shared edit tokens across same-domain services — one token manages all your listings
- JSON API for programmatic access and agent queries
- Premium endpoints (bulk export, analytics, reputation) gated via L402 macaroons
- Health probing and service status tracking (live / confirmed / dead / unverified)

## Quick Start

```bash
git clone https://github.com/toadlyBroodle/satring.git
cd satring
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # configure wallet keys, or leave defaults for test mode
uvicorn app.main:app --reload
```

Open http://localhost:8000

In test mode (`AUTH_ROOT_KEY=test-mode`), all payment gates are bypassed.

## API

Full interactive docs at [satring.com/docs](https://satring.com/docs).

### Free endpoints

```bash
# List services (paginated, filterable by category)
curl https://satring.com/api/v1/services?category=search&page=1&page_size=20

# Search
curl https://satring.com/api/v1/search?q=satring

# Service details
curl https://satring.com/api/v1/services/my-service

# List ratings
curl https://satring.com/api/v1/services/my-service/ratings
```

### L402-gated endpoints

These require an L402 payment token in the `Authorization` header:

```bash
# Submit a service
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My API",
    "url": "https://api.example.com",
    "pricing_sats": 10,
    "pricing_model": "per-request",
    "protocol": "L402",
    "category_ids": [1, 2]
  }'

# Link to existing service on same domain
curl -X POST https://satring.com/api/v1/services \
  -H "Authorization: L402 <macaroon>:<preimage>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My Other API",
    "url": "https://api.example.com/v2",
    "existing_edit_token": "your-token-from-first-service"
  }'

# Edit a service
curl -X PATCH https://satring.com/api/v1/services/my-api \
  -H "X-Edit-Token: your-edit-token" \
  -H "Content-Type: application/json" \
  -d '{"description": "Updated description"}'

# Bulk export, analytics, reputation
curl https://satring.com/api/v1/services/bulk
curl https://satring.com/api/v1/analytics
curl https://satring.com/api/v1/services/my-service/reputation
```

### Token recovery

If you lose your edit token, or want to edit a pre-seeded endpoint, prove domain ownership to get a new one:

```bash
# 1. Generate a challenge
curl -X POST https://satring.com/api/v1/services/my-service/recover/generate

# 2. Place the challenge code at your-domain.com/.well-known/satring-verify

# 3. Verify — new token is applied to ALL services on the same domain
curl -X POST https://satring.com/api/v1/services/my-service/recover/verify
```

## Configuration

Environment variables (see `.env`):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite+aiosqlite:///./db/sr.db` | Database connection string |
| `PAYMENT_URL` | — | Wallet instance URL |
| `PAYMENT_KEY` | — | Wallet invoice/read key |
| `AUTH_ROOT_KEY` | `test-mode` | Set to wallet key for production; `test-mode` bypasses payments |
| `AUTH_SUBMIT_PRICE_SATS` | `1000` | Cost to submit a service |
| `AUTH_REVIEW_PRICE_SATS` | `10` | Cost to submit a review |
| `AUTH_BULK_PRICE_SATS` | `1000` | Cost for bulk export |
| `AUTH_PRICE_SATS` | `100` | Default price for premium endpoints |

## Tech Stack

- **FastAPI** + **Jinja2** + **HTMX** — server-rendered with progressive enhancement
- **SQLAlchemy** (async) + **SQLite** — simple, no external DB needed
- **L402 / Macaroons** — Lightning-native authentication via [pymacaroons](https://github.com/ecordell/pymacaroons)
- **Tailwind CSS** (browser CDN) — terminal-themed green-on-black UI

## Contributing

All contributions welcome!

## License

[MIT](LICENSE)
