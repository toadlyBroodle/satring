import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service


def slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def unique_slug(db: AsyncSession, text: str) -> str:
    base = slugify(text)
    slug = base
    result = await db.execute(select(Service).where(Service.slug == slug))
    if result.scalars().first() is None:
        return slug
    counter = 1
    while True:
        slug = f"{base}-{counter}"
        result = await db.execute(select(Service).where(Service.slug == slug))
        if result.scalars().first() is None:
            return slug
        counter += 1
