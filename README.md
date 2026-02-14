```
 ___  __ _| |_ _ __(_)_ __   __ _
/ __|/ _` | __| '__| | '_ \ / _` |
\__ \ (_| | |_| |  | | | | | (_| |
|___/\__,_|\__|_|  |_|_| |_|\__, |
                             |___/  ⚡ L402
```

[satring.com](https://satring.com) — the L402 service directory. Find, rate, and connect to Lightning-paywalled APIs.

Satring helps AI agents and developers discover L402 services — APIs that accept Bitcoin Lightning payments via the [L402 protocol](https://www.l402.org/). Browse the directory, submit your service, and let agents find you.

## Why

AI agents can now [pay for APIs autonomously](https://lightning.engineering/posts/2026-02-11-ln-agent-tools/) using Lightning. But there's no way to discover what's available. Satring is the missing directory.

## Features

- Browse and search L402-enabled APIs
- Submit your L402 service to the directory
- Ratings and reputation scores
- JSON API for programmatic agent queries
- Freemium — basic discovery is free, premium features via L402

## Quick Start

```bash
git clone https://github.com/toadlyBroodle/satring.git
cd satring
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://localhost:8000

## API

```bash
# List all services
curl https://satring.dev/api/v1/services

# Search by category
curl https://satring.dev/api/v1/services?category=data

# Get service details
curl https://satring.dev/api/v1/services/{id}
```

Agents using [lnget](https://github.com/lightninglabs/lightning-agent-tools) can query the API directly to discover L402 endpoints.

## Submit a Service

List your L402 API in the directory:

1. Visit https://satring.dev/submit
2. Provide your endpoint URL, description, pricing, and category
3. Your service appears in the directory after verification

Or submit via API:

```bash
curl -X POST https://satring.dev/api/v1/services \
  -H "Content-Type: application/json" \
  -d '{"name": "My API", "url": "https://api.example.com", "price_sats": 10, "category": "data"}'
```

## Contributing

All contributions welcome!

## License

[MIT](LICENSE)
