# Satring — Technical Stack

## Stack

- **Frontend**: Jinja2 + HTMX + DaisyUI (zero JS, CDN-only)
- **Backend**: FastAPI (Python 3.13)
- **Database**: SQLite + SQLAlchemy 2.0 (Postgres-ready via connection string swap)
- **L402**: pylsat + LNbits/Alby for invoice generation
- **Deploy**: Fly.io + Litestream for SQLite backup

## Project Structure

```
satring/
  app/
    main.py              # FastAPI app
    config.py            # Settings / env vars
    database.py          # SQLAlchemy session
    models.py            # Service, User, Rating, Category
    l402.py              # pylsat middleware
    routes/
      web.py             # HTML responses (browsers)
      api.py             # JSON responses (agents)
    templates/
      base.html          # DaisyUI layout + HTMX script
      services/
        list.html        # Directory listing
        detail.html      # Service detail + ratings
        _card.html       # HTMX partial for search
        submit.html      # Register a new service
  satring.db             # SQLite (gitignored)
  requirements.txt
```

## Model (Freemium)

- **Free**: Browse/search L402 services, view ratings, submit listings
- **Premium (L402-gated)**: Full API access, bulk queries, analytics, detailed reputation scores

## Build Order

1. Scaffold FastAPI + templates + DB models
2. Service CRUD (list, detail, submit)
3. Search with HTMX live filtering
4. Rating/reputation system
5. API routes (JSON mirror of web routes)
6. L402 paywall on premium endpoints
7. Deploy
