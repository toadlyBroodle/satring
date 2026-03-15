```
 ___  __ _| |_ _ __(_)_ __   __ _
/ __|/ _` | __| '__| | '_ \ / _` |
\__ \ (_| | |_| |  | | | | | (_| |
|___/\__,_|\__|_|  |_|_| |_|\__, |
                             |___/  ⚡ L402 + x402
```

<img src="app/static/img/satring-logo-trans-bg.png" alt="satring" width="20"> [satring.com](https://satring.com) — the dual-protocol paid API directory. Find, rate, and connect to paywalled APIs via Lightning (L402) or USDC on Base (x402).

Satring helps AI agents and developers discover paid API services that accept payments via the [L402 protocol](https://www.l402.org/) (Bitcoin Lightning) or the [x402 protocol](https://www.x402.org/) (USDC on Base). Browse the directory, submit your service, and let agents find you.

## Why

AI agents can now [pay for APIs autonomously](https://lightning.engineering/posts/2026-02-11-ln-agent-tools/) using Lightning. But there's no way to discover what's available. Satring is the missing directory.

## Features

- Browse, search, and filter paid APIs by category, status, and protocol
- **Dual-protocol payments**: L402 (Bitcoin Lightning) and x402 (USDC on Base) supported side by side
- Submit services with payment gate (anti-spam), payable via either protocol
- Ratings and reputation system (also payment-gated)
- Edit your listing with secure edit tokens
- Recover lost edit tokens via domain verification (`.well-known/satring-verify`)
- Shared edit tokens across same-domain services; one token manages all your listings
- JSON API for programmatic access and agent queries
- Premium endpoints (bulk export, analytics, reputation) gated via L402 or x402
- Per-service health analytics: uptime percentage, average latency, probe history
- Health probing with automatic protocol detection (L402, x402, or both)
- Service status tracking (live / confirmed / dead)

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
# List services (paginated, filterable by category, status, and protocol)
curl "https://satring.com/api/v1/services?category=search&status=live&protocol=L402&page=1&page_size=20"

# Search (also filterable by status and protocol)
curl "https://satring.com/api/v1/search?q=satring&protocol=X402"

# Service details
curl https://satring.com/api/v1/services/my-service

# List ratings
curl https://satring.com/api/v1/services/my-service/ratings
```

### Payment-gated endpoints

These require payment via **L402** or **x402**. Without auth headers, the server returns `402` with challenges for both protocols.

#### Option A: L402 (Lightning)

```bash
# Submit a service via L402
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
```

#### Option B: x402 (USDC on Base)

```bash
# Submit a service via x402
curl -X POST https://satring.com/api/v1/services \
  -H "PAYMENT-SIGNATURE: <base64-encoded-payment-json>" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "My API",
    "url": "https://api.example.com",
    "pricing_usd": "0.50",
    "pricing_model": "per-request",
    "protocol": "x402",
    "x402_network": "eip155:8453",
    "x402_asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "x402_pay_to": "0xYourWalletAddress",
    "category_ids": [1, 2]
  }'
```

#### Other gated operations

```bash
# Link to existing service on same domain (L402 or x402 auth)
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

# Bulk export (use -i to see payment challenges in headers)
curl -i https://satring.com/api/v1/services/bulk

# Directory analytics (totals, health overview, pricing stats by protocol, growth, leaderboards, route usage)
curl -i https://satring.com/api/v1/analytics

# Per-service health analytics (uptime %, avg latency, probe history)
curl -i https://satring.com/api/v1/services/my-service/analytics

# Service reputation (rating distribution, monthly trends, peer comparison, review activity)
curl -i https://satring.com/api/v1/services/my-service/reputation
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
| `AUTH_BULK_PRICE_SATS` | `5000` | Cost for bulk export |
| `AUTH_ANALYTICS_PRICE_SATS` | `500` | Cost for directory analytics |
| `AUTH_SERVICE_ANALYTICS_PRICE_SATS` | `50` | Cost for per-service health analytics |
| `AUTH_REPUTATION_PRICE_SATS` | `100` | Cost for reputation lookup |
| `AUTH_PRICE_SATS` | `100` | Default price for premium endpoints |
| `X402_FACILITATOR_URL` | `https://facilitator.xpay.sh` | x402 facilitator endpoint |
| `X402_PAY_TO` | — | USDC wallet address (empty = x402 disabled) |
| `X402_NETWORK` | `eip155:8453` | Chain ID (default: Base mainnet) |
| `X402_ASSET` | `0x8335...2913` | USDC contract address on Base |
| `AUTH_SUBMIT_PRICE_USD` | `0.50` | x402 cost to submit a service |
| `AUTH_REVIEW_PRICE_USD` | `0.01` | x402 cost to submit a review |
| `AUTH_BULK_PRICE_USD` | `2.50` | x402 cost for bulk export |
| `AUTH_ANALYTICS_PRICE_USD` | `0.25` | x402 cost for directory analytics |
| `AUTH_SERVICE_ANALYTICS_PRICE_USD` | `0.025` | x402 cost for per-service health analytics |
| `AUTH_REPUTATION_PRICE_USD` | `0.05` | x402 cost for reputation lookup |

## Tech Stack

- **FastAPI** + **Jinja2** + **HTMX**: server-rendered with progressive enhancement
- **SQLAlchemy** (async) + **SQLite**: simple, no external DB needed
- **L402 / Macaroons**: Lightning-native authentication via [pymacaroons](https://github.com/ecordell/pymacaroons)
- **x402 / USDC on Base**: stablecoin payments via [xpay.sh](https://xpay.sh) facilitator
- **Tailwind CSS** (browser CDN): terminal-themed green-on-black UI

## Contributing

All contributions welcome!

## License

[MIT](LICENSE)
