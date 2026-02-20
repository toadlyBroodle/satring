from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import init_db, async_session
from app.models import Category

# SECURITY: Rate limiter to prevent abuse and DoS. Applied per-endpoint in route files.
limiter = Limiter(key_func=get_remote_address)


class OriginCheckMiddleware(BaseHTTPMiddleware):
    """SECURITY: Reject cross-origin POST/PUT/DELETE/PATCH requests.
    Prevents CSRF by verifying the Origin header matches BASE_URL.
    This app has no session cookies so CSRF risk is limited, but this
    is a low-cost defense-in-depth measure."""

    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("origin")
            if origin:
                allowed = urlparse(settings.BASE_URL).netloc
                actual = urlparse(origin).netloc
                if actual != allowed:
                    if request.url.path.startswith("/api/"):
                        return JSONResponse(
                            {"detail": "Cross-origin request blocked"},
                            status_code=403,
                        )
                    return HTMLResponse("Cross-origin request blocked", status_code=403)
        return await call_next(request)

SEED_CATEGORIES = [
    ("AI / ML", "ai-ml", "Machine learning and AI inference APIs"),
    ("Data", "data", "Data feeds, aggregation, and analytics"),
    ("Finance", "finance", "Financial data, trading, and payment APIs"),
    ("Identity", "identity", "KYC, authentication, and verification"),
    ("Media", "media", "Image, video, and audio processing"),
    ("Search", "search", "Web search, indexing, and discovery"),
    ("Social", "social", "Social networks, communications, and notification APIs"),
    ("Storage", "storage", "File storage and content delivery"),
    ("Tools", "tools", "Developer tools, utilities, and infrastructure"),
]


async def seed_categories():
    async with async_session() as db:
        result = await db.execute(select(Category).limit(1))
        if result.scalars().first() is not None:
            return
        for name, slug, description in SEED_CATEGORIES:
            db.add(Category(name=name, slug=slug, description=description))
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await seed_categories()
    yield


app = FastAPI(title="satring", description="L402 Service Directory", lifespan=lifespan, docs_url=None)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(OriginCheckMiddleware)


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui():
    return HTMLResponse("""<!DOCTYPE html>
<html><head>
<meta charset="UTF-8">
<title>satring — API Docs</title>
<link rel="icon" type="image/png" href="/static/img/satring-logo-trans-bg.png">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
<link rel="stylesheet" href="/static/css/theme.css">
<style>body { transition: opacity 0.15s; }</style>
</head><body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
<script>
SwaggerUIBundle({
  url: "/openapi.json",
  dom_id: "#swagger-ui",
  presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
  plugins: [SwaggerUIBundle.plugins.DownloadUrl],
  layout: "StandaloneLayout",
  syntaxHighlight: { theme: "monokai" },
  deepLinking: true,
});
document.body.style.opacity = "1";
</script>
</body></html>""")

from pathlib import Path

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

from app.routes.web import router as web_router   # noqa: E402
from app.routes.api import router as api_router    # noqa: E402

app.mount("/.well-known", StaticFiles(directory=Path(__file__).parent / ".well-known"), name="well-known")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(web_router)
app.include_router(api_router, prefix="/api/v1")
