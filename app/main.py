from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from app.database import init_db, async_session
from app.models import Category

SEED_CATEGORIES = [
    ("AI / ML", "ai-ml", "Machine learning and AI inference APIs"),
    ("Data", "data", "Data feeds, aggregation, and analytics"),
    ("Finance", "finance", "Financial data, trading, and payment APIs"),
    ("Identity", "identity", "KYC, authentication, and verification"),
    ("Media", "media", "Image, video, and audio processing"),
    ("Messaging", "messaging", "Email, SMS, and push notification APIs"),
    ("Search", "search", "Web search, indexing, and discovery"),
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


app = FastAPI(title="Satring", description="L402 Service Directory", lifespan=lifespan)

from pathlib import Path

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

from app.routes.web import router as web_router   # noqa: E402
from app.routes.api import router as api_router    # noqa: E402

app.include_router(web_router)
app.include_router(api_router, prefix="/api/v1")
