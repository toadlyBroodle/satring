import hashlib
import hmac
import re
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service


def slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def generate_edit_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_edit_token(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(plaintext), stored_hash)


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
