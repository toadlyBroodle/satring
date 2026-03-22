import hashlib
import hmac
import ipaddress
import logging
import os
from pathlib import Path
import re
import secrets
import smtplib
import socket
from email.mime.text import MIMEText
from urllib.parse import urlparse

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Service, Category, Rating


BASE_PROTOCOLS = ("L402", "x402", "MPP")
_PROTO_ORDER = {"L402": 0, "x402": 1, "MPP": 2}


def canonical_protocol(*parts: str) -> str:
    """Sort protocol parts into canonical order and join with '+'."""
    return "+".join(sorted(set(parts), key=lambda p: _PROTO_ORDER.get(p, 99)))


def is_valid_protocol(protocol: str) -> bool:
    """Check if a '+'-joined protocol string contains only valid base protocols in canonical order."""
    if not protocol:
        return False
    parts = protocol.split("+")
    if not parts or not all(p in _PROTO_ORDER for p in parts):
        return False
    if len(parts) != len(set(parts)):
        return False
    return protocol == canonical_protocol(*parts)


# Keep for backwards compatibility in imports
VALID_PROTOCOLS = ("L402", "x402", "MPP", "L402+x402", "L402+MPP", "x402+MPP", "L402+x402+MPP")


def normalize_protocol(protocol: str | None) -> str | None:
    """Normalize a protocol query param (URL decodes '+' as space) and validate."""
    if not protocol:
        return None
    protocol = protocol.replace(" ", "+")
    return protocol if is_valid_protocol(protocol) else None


def protocol_filter(column, protocol: str):
    """Return a SQLAlchemy filter clause for protocol matching.

    Single-protocol filter (e.g. "MPP") matches any combo containing it.
    Multi-protocol filter (e.g. "L402+x402") matches only that exact combo.
    """
    if "+" in protocol:
        return column == protocol
    # Match any protocol string that contains this base protocol
    matching = [p for p in VALID_PROTOCOLS if protocol in p.split("+")]
    return column.in_(matching)


def escape_like(s: str, escape: str = "\\") -> str:
    """SECURITY: Escape SQL LIKE metacharacters (%, _, \\) in user input
    so they are matched literally instead of acting as wildcards."""
    s = s.replace(escape, escape + escape)
    s = s.replace("%", escape + "%")
    s = s.replace("_", escape + "_")
    return s


def slugify(text: str) -> str:
    slug = text.lower().strip()
    # Turn punctuation that separates words (dots, slashes, colons) into spaces
    # so they become dashes rather than being silently removed
    slug = re.sub(r"[./:]+", " ", slug)
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


def domain_root(url: str) -> str:
    """Return scheme://hostname for a URL (no path, no port)."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname}" if parsed.hostname else url


def is_public_hostname(hostname: str) -> bool:
    """SECURITY: Return True only if hostname resolves to a public IP.
    Used to prevent SSRF by blocking requests to loopback, link-local
    (e.g. AWS metadata 169.254.x.x), and private network ranges."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        # Not a literal IP — resolve via DNS
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(hostname))
        except (socket.gaierror, ValueError):
            return False
    return ip.is_global


async def get_same_domain_services(db: AsyncSession, url: str) -> list[Service]:
    """Return all services whose URL is on the same domain as `url`."""
    domain = extract_domain(url)
    if not domain:
        return []
    # Broad LIKE filter, then exact post-filter on parsed hostname
    result = await db.execute(
        select(Service).where(Service.url.ilike(f"%{escape_like(domain)}%", escape="\\")).where(Service.status != "purged")
    )
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


def normalize_url(url: str) -> str:
    """Normalize URL for dedup: sort query params, strip fragments and trailing slashes."""
    try:
        parsed = urlparse(url)
        # Preserve query params but sort them for consistent comparison
        from urllib.parse import parse_qsl, urlencode
        sorted_query = urlencode(sorted(parse_qsl(parsed.query)))
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
        return f"{base}?{sorted_query}" if sorted_query else base
    except ValueError:
        return url.rstrip("/")


async def find_existing_service(db: AsyncSession, url: str) -> Service | None:
    """Find a non-purged service with the same normalized URL, if any."""
    norm = normalize_url(url)
    result = await db.execute(
        select(Service).where(Service.url == norm, Service.status != "purged")
    )
    svc = result.scalars().first()
    if svc:
        return svc
    # Also check un-normalized form (trailing slash variants)
    result = await db.execute(
        select(Service).where(Service.url == url, Service.status != "purged")
    )
    return result.scalars().first()


async def find_purged_service(db: AsyncSession, url: str) -> Service | None:
    """Find a purged service with the given URL, if any."""
    result = await db.execute(
        select(Service).where(Service.url == url, Service.status == "purged")
    )
    return result.scalars().first()


async def overwrite_purged_service(
    db: AsyncSession,
    service: Service,
    *,
    name: str,
    slug: str,
    description: str = "",
    pricing_sats: int = 0,
    pricing_model: str = "per-request",
    protocol: str = "L402",
    owner_name: str = "",
    owner_contact: str = "",
    logo_url: str = "",
    edit_token_hash: str | None = None,
    status: str = "unverified",
    category_ids: list[int] | None = None,
    domain_verified: bool = False,
    domain_challenge: str | None = None,
    x402_network: str | None = None,
    x402_asset: str | None = None,
    x402_pay_to: str | None = None,
    pricing_usd: str | None = None,
    mpp_method: str | None = None,
    mpp_realm: str | None = None,
    mpp_currency: str | None = None,
) -> None:
    """Overwrite a purged service's fields for re-submission. Preserves ratings."""
    service.name = name
    service.slug = slug
    service.description = description
    service.pricing_sats = pricing_sats
    service.pricing_model = pricing_model
    service.protocol = protocol
    service.owner_name = owner_name
    service.owner_contact = owner_contact
    service.logo_url = logo_url
    service.x402_network = x402_network
    service.x402_asset = x402_asset
    service.x402_pay_to = x402_pay_to
    service.pricing_usd = pricing_usd
    service.mpp_method = mpp_method
    service.mpp_realm = mpp_realm
    service.mpp_currency = mpp_currency
    service.status = status
    service.dead_since = None
    service.last_probed_at = None
    service.domain_verified = domain_verified
    service.domain_challenge = domain_challenge
    service.domain_challenge_expires_at = None
    if edit_token_hash is not None:
        service.edit_token_hash = edit_token_hash

    # Update categories
    if category_ids is not None:
        cats = (await db.execute(
            select(Category).where(Category.id.in_(category_ids))
        )).scalars().all()
        service.categories = list(cats)

    # Recalculate avg_rating / rating_count from preserved ratings
    avg_result = await db.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.service_id == service.id)
    )
    row = avg_result.one()
    service.avg_rating = round(float(row[0]), 1) if row[0] else 0.0
    service.rating_count = row[1] or 0


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_log = logging.getLogger(__name__)


def extract_email(s: str) -> str | None:
    """Extract the first valid email address from a string, or None."""
    match = _EMAIL_RE.search(s)
    return match.group(0) if match else None


_VERIFY_EMAIL_TEMPLATE = Path(__file__).resolve().parent.parent / "docs" / "coms" / "email-new-registration-verify.txt"


def send_verify_email(email: str, slug: str, domain: str) -> None:
    """Send domain verification instructions via local postfix.

    Loads template from docs/coms/email-new-registration-verify.txt and
    substitutes SERVICE_SLUG and YOUR_DOMAIN placeholders.
    Designed to run in a BackgroundTask so it never blocks the response.
    Skipped in test mode.
    """
    from app.config import payments_enabled
    if not payments_enabled():
        return

    try:
        template = _VERIFY_EMAIL_TEMPLATE.read_text(encoding="utf-8")
    except FileNotFoundError:
        _log.error(f"Verification email template not found: {_VERIFY_EMAIL_TEMPLATE}")
        return

    body = template.replace("SERVICE_SLUG", slug).replace("YOUR_DOMAIN", domain)

    msg = MIMEText(body)
    msg["Subject"] = "Verify your service on satring.com"
    msg["From"] = os.getenv("MAIL_FROM", "noreply@satring.com")
    msg["To"] = email

    try:
        with smtplib.SMTP("localhost") as srv:
            srv.send_message(msg)
        _log.info(f"Sent verification email to {email} for {slug}")
    except Exception:
        _log.exception(f"Failed to send verification email to {email}")
