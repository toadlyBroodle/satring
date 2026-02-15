import hashlib
import hmac
import re
import secrets
from urllib.parse import urlparse

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


def extract_domain(url: str) -> str:
    """Extract the hostname from a URL."""
    return urlparse(url).hostname or ""


async def get_same_domain_services(db: AsyncSession, url: str) -> list[Service]:
    """Return all services whose URL is on the same domain as `url`."""
    domain = extract_domain(url)
    if not domain:
        return []
    # Broad LIKE filter, then exact post-filter on parsed hostname
    result = await db.execute(select(Service).where(Service.url.ilike(f"%{domain}%")))
    return [s for s in result.scalars().all() if extract_domain(s.url) == domain]


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
