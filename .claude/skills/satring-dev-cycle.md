---
name: satring-dev-cycle
description: Satring-specific development iteration. Read docs/SPEC.md for next items, implement backend+web, run ALL tests (must pass), commit, deploy to VPS, Playwright browser test, update SPEC.md results.
user-invocable: true
---

# Satring Development Cycle

Full iteration for the satring paid API directory. Each cycle picks the next unchecked items from `docs/SPEC.md`, implements across backend and frontend, runs all tests, commits only if tests pass, deploys, and verifies in browser.

## 1. Find Next Work

1. Read `docs/SPEC.md` and scan for unchecked `[ ]` items. Phases are numbered.
2. Use an Explore agent to compare spec against actual implementation. Check:
   - `app/routes/api.py` for JSON API endpoints (prefix `/api/v1`)
   - `app/routes/web.py` for HTML routes, form handlers, well-known endpoints
   - `app/models.py` for DB schema
   - `app/payment.py` for tri-protocol payment gate (`require_payment()`)
   - `app/l402.py`, `app/x402.py`, `app/mpp.py` for protocol-specific logic
   - `app/health.py` for background service probing
   - `app/usage.py` for usage tracking and agent classification
   - `app/config.py` for settings, rate limits, price constants
   - `app/main.py` for middleware, OpenAPI customization, app factory
   - `app/templates/` for Jinja2 templates (HTMX frontend)
   - `app/static/css/theme.css` for terminal dark theme CSS variables
   - `tests/` for test coverage
3. Spec checkboxes may be stale. The API may be done but the web UI missing, or vice versa.
4. Present the gap to the user and confirm what to implement.

## 2. Implement

### Task tracking
- Create a task per discrete change.
- Mark tasks in_progress when starting, completed when done.

### Backend (`app/`)
- API endpoints go in `app/routes/api.py` under the existing `router`.
- Web routes go in `app/routes/web.py` (HTML responses, form handlers).
- Shared data builders (`build_analytics_data`, `build_reputation_data`) live in `api.py` and are imported by `web.py`.
- Rate-limit every endpoint via `@limiter.limit()` using constants from `config.py`.
- DB models in `app/models.py`. Migrations in `app/database.py` (`init_db()` adds columns via `ALTER TABLE` if missing).
- Payment pattern: `require_payment()` in route dependency. Handles L402, x402, and MPP automatically based on request headers. Returns 402 with protocol challenges when no auth present.
- New config values go in `app/config.py` as class attributes on `Settings`, with env var fallbacks.
- Pydantic response models are defined inline in `api.py` alongside the routes that use them.

### Frontend (`app/templates/` + `app/static/`)
- Jinja2 templates with HTMX for dynamic updates. No JS framework.
- Terminal dark theme: green-on-black using Tailwind + CSS variables from `app/static/css/theme.css`.
- Use CSS variables (`--text`, `--bg`, `--accent`, `--border`, `--dim`, `--error`) for all colors.
- Payment widget: shows QR + BOLT11, polls `/payment-status/{hash}` every 5s.

### Security patterns
- Comments prefixed `SECURITY:` mark critical code. Follow existing patterns.
- Input length limits: constants in `config.py`, enforced server-side.
- URL scheme validation: reject non-http(s) to prevent stored XSS.
- SSRF: `is_public_hostname()` before any server-side HTTP fetch.
- LIKE injection: `escape_like()` before any `.ilike()` call.

## 3. Test (GATE: must pass before step 4)

**CRITICAL: Do NOT commit or deploy until ALL tests pass. If tests fail, fix the code and re-run until green.**

```bash
python -m pytest --tb=short -q
```

Expect 448+ tests across 20 files, ~45s. Key test files:
- `test_api.py`: Core API CRUD operations
- `test_endpts.py`, `test_endpt_smoke.py`: Response schema validation
- `test_web.py`: Web route and form handling
- `test_l402.py`: L402 macaroon/invoice flow
- `test_x402.py`: x402 facilitator integration
- `test_mpp.py`: MPP challenge/credential verification
- `test_payment.py`: Multi-protocol payment gate
- `test_paywall.py`: Paywall enforcement across protocols
- `test_health.py`: Background service probing
- `test_security.py`: Input validation, injection prevention
- `test_usage.py`: Usage tracking, agent classification
- `test_mcp.py`: MCP tool integration

Test fixtures in `tests/conftest.py`:
- `client`: AsyncClient with fresh in-memory SQLite (function-scoped)
- `sample_service`, `sample_x402_service`, `sample_mpp_service`: Pre-created services
- All tests run with `AUTH_ROOT_KEY=test-mode` (payment gates disabled)

If any test fails:
1. Read the failure output carefully
2. Fix the code (not the test, unless the test expectation is wrong)
3. Re-run `python -m pytest --tb=short -q`
4. Repeat until 0 failures
5. Only then proceed to step 4

## 4. Commit

### Public/private segregation check (CRITICAL)

**This repo is open-source. Many directories are gitignored because they contain proprietary or security-sensitive content.**

Before staging, verify NO gitignored files are included:
- `docs/` - Internal specs, roadmaps, promo, market research
- `deploy/` - Server configs, deploy scripts, infra details, wallet keys
- `app/.well-known/*` - Nostr keys, verification hashes, discovery files with wallet addresses
- `tests/test_paywall.py` - Payment gate internals
- `.env`, `db/`, `.creds`, `logs/` - Secrets, databases, credentials

**Never use `git add -f` on gitignored files.** If a new file naturally belongs in a gitignored directory, keep it local-only. Only files in public directories (`app/` source, `tests/` except test_paywall.py, `mcp/`, `.claude/skills/`, root config files) should be committed.

Run `git status` after staging and before committing. If any gitignored path appears staged, unstage it.

### Commit

```bash
git add <specific files>
git commit -m "$(cat <<'EOF'
Subject line in imperative mood

Body with details.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>
EOF
)"
```

Commit style: short subject in imperative mood, optional body paragraph, Co-Authored-By trailer.
**Always ask the user before committing. Never auto-commit.**

## 5. Deploy

**Always ask the user before deploying. Never auto-deploy.**

### 5a. Push public code and deploy
```bash
git push origin $(git branch --show-current)
ssh vps "cd /home/rob/Dev/satring && git pull && python deploy/update_satring.py deploy"
```

The deploy script (`deploy/update_satring.py deploy`):
1. Pulls latest code
2. Installs pip deps
3. Backs up SQLite DB to `db/backups/`
4. Seeds Satring's own API endpoints into the directory
5. Restarts `satring.service` via systemd

### 5b. SCP private files if changed
Private files (gitignored) are NOT deployed via git push. If any were modified locally and are required for VPS operations, manually scp them. Only copy files strictly required for the service to run:

```bash
# Only if changed and required for production:
scp deploy/update_satring.py vps:~/Dev/satring/deploy/
scp deploy/satring.nginx vps:~/Dev/satring/deploy/
scp deploy/satring-ratelimit.conf vps:~/Dev/satring/deploy/
scp deploy/satring.service vps:~/Dev/satring/deploy/
scp app/.well-known/agent.json vps:~/Dev/satring/app/.well-known/
scp docs/SPEC.md vps:~/Dev/satring/docs/  # only if deploy script references it
```

Do NOT scp files that are only used locally (promo materials, market research, demo scripts). Only transfer what the VPS needs to serve requests.

After scp, restart if config changed:
```bash
ssh vps "sudo systemctl restart satring.service"
```

### 5c. Verify
```bash
ssh vps "systemctl status satring.service"
```
Confirm `Active: active (running)`.

## 6. Playwright Browser Test

Test against production at `https://satring.com`:

### Verify core pages
1. Navigate to `https://satring.com` (stats landing page)
2. Check stats render correctly (service counts, protocol breakdown)
3. Navigate to `/directory` and verify service listings load
4. Click a service to verify detail page renders
5. Navigate to `/docs` and verify Swagger UI loads
6. Navigate to `/submit` and verify form renders

### Verify agent discovery endpoints
1. Check `/.well-known/agent.json` returns valid JSON
2. Check `/.well-known/x402` returns x402 discovery data
3. Check `/.well-known/l402` returns L402 discovery data
4. Check `/.well-known/mpp` returns MPP discovery data
5. Check `/openapi.json` returns valid OpenAPI schema

### Verify API
1. Check `/api/v1/services` returns paginated service list
2. Check `/api/v1/categories` returns category list
3. Check `/api/v1/search?q=test` returns search results

### Check for errors
- `browser_console_messages` level `error`: target 0 errors.
- Snapshots show correct element refs and content.

## 7. Update SPEC.md

1. In `docs/SPEC.md`, change `[ ]` to `[x]` for completed items. Add `[DONE]` to section headers.
2. Update the `## Test Results` section:
   - Update date, total test count, per-file breakdown
   - Add Playwright test results with specific observations
3. Keep all spec content intact. Only update checkboxes and status markers.
4. Leave SPEC.md changes unstaged. They'll be included in the next iteration's commit.

## Reference: Project Structure

```
app/
  main.py           - FastAPI app, lifespan, security middleware, OpenAPI customization
  config.py         - Env settings, payments_enabled(), x402_enabled(), rate limits
  database.py       - Async SQLAlchemy + migrations
  models.py         - Service, Category, Rating, RouteUsage, AgentUsage, ProbeHistory, ConsumedPayment
  payment.py        - Unified multi-protocol payment gate (require_payment)
  l402.py           - Macaroon minting/verification, LNbits invoice creation
  x402.py           - x402 protocol: challenges, signatures, facilitator calls
  mpp.py            - MPP: HMAC-bound challenges, credential verification, receipts
  health.py         - Background service liveness probing
  usage.py          - Usage tracking, agent classification, buffer flush
  utils.py          - Slugification, edit tokens, domain extraction, SSRF protection
  routes/
    api.py          - JSON API (prefix /api/v1): services, search, analytics, ratings
    web.py          - HTML pages: directory, submit, edit, stats, well-known, llms.txt
  templates/        - Jinja2 templates (HTMX frontend)
  static/
    css/theme.css   - Terminal dark theme CSS variables
    img/            - Logo and assets
  .well-known/      - Static discovery files (agent.json, nostr.json)
tests/              - 20 test files, 448+ tests
deploy/
  update_satring.py - VPS: setup, deploy, backup, seed, restart, monitor
  satring.service   - systemd unit
  satring.nginx     - nginx reverse proxy
  satring-ratelimit.conf - nginx rate limit zones
  monitor/          - Traffic monitoring and reporting cron jobs
  wallet/           - x402 USDC wallet management
mcp/
  satring_mcp.py    - MCP server for agent service discovery
docs/
  SPEC.md           - Implementation spec with phase checklists
```
