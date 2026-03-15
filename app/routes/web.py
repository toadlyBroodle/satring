import math
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Request, Depends, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import (
    settings, payments_enabled, MAX_NAME, MAX_URL, MAX_DESCRIPTION, MAX_OWNER_NAME,
    MAX_OWNER_CONTACT, MAX_LOGO_URL, MAX_REVIEWER_NAME, MAX_COMMENT,
    MAX_X402_NETWORK, MAX_X402_ASSET, MAX_X402_PAY_TO, MAX_PRICING_USD,
    RATE_SUBMIT, RATE_EDIT, RATE_DELETE, RATE_RECOVER, RATE_REVIEW,
    RATE_SEARCH, RATE_PAYMENT_STATUS, RATE_SITEMAP,
)
from app.database import get_db
from app.l402 import create_invoice, check_payment_status, check_and_consume_payment
from app.main import templates, limiter
from app.models import Service, Category, Rating, service_categories
from app.routes.api import build_reputation_data, build_analytics_data, build_service_analytics
from app.utils import unique_slug, generate_edit_token, hash_token, verify_edit_token, get_same_domain_services, domain_root, extract_domain, is_public_hostname, extract_email, send_verify_email, find_purged_service, find_existing_service, normalize_url, overwrite_purged_service, escape_like

router = APIRouter(include_in_schema=False)

PAGE_SIZE = 20


@router.get("/", response_class=HTMLResponse)
async def directory(
    request: Request,
    q: str = "",
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    verified: str | None = None,
    protocol: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()

    query = select(Service).options(selectinload(Service.categories)).where(Service.status != "purged")
    if q.strip():
        # SECURITY: escape LIKE wildcards so user input is matched literally
        pattern = f"%{escape_like(q.strip())}%"
        query = query.where(Service.name.ilike(pattern, escape="\\") | Service.description.ilike(pattern, escape="\\"))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)
    if verified == "true":
        query = query.where(Service.domain_verified == True)
    if protocol and protocol in ("L402", "X402"):
        query = query.where(Service.protocol == protocol)

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
    }
    query = query.order_by(sort_map.get(sort, Service.created_at.desc()))

    # Count total for pagination
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)

    services = (await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()

    # Build qs_base for pagination links (preserving existing filters)
    qs_parts = []
    if q.strip():
        qs_parts.append(f"q={q.strip()}")
    if category:
        qs_parts.append(f"category={category}")
    if verified == "true":
        qs_parts.append("verified=true")
    elif status:
        qs_parts.append(f"status={status}")
    if protocol and protocol in ("L402", "X402"):
        qs_parts.append(f"protocol={protocol}")
    if sort and sort != "newest":
        qs_parts.append(f"sort={sort}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(request, "services/list.html", {
        "services": services,
        "categories": categories,
        "active_category": category,
        "active_status": status,
        "active_sort": sort,
        "active_verified": verified,
        "active_protocol": protocol,
        "active_q": q.strip(),
        "page": page,
        "total_pages": total_pages,
        "qs_base": qs_base,
        "analytics_price_sats": settings.AUTH_ANALYTICS_PRICE_SATS,
    })


@router.get("/search", response_class=HTMLResponse)
@limiter.limit(RATE_SEARCH)
async def search(
    request: Request,
    q: str = "",
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    verified: str | None = None,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).options(selectinload(Service.categories)).where(Service.status != "purged")
    if q.strip():
        # SECURITY: escape LIKE wildcards so user input is matched literally
        pattern = f"%{escape_like(q.strip())}%"
        query = query.where(Service.name.ilike(pattern, escape="\\") | Service.description.ilike(pattern, escape="\\"))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)
    if verified == "true":
        query = query.where(Service.domain_verified == True)

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
    }
    query = query.order_by(sort_map.get(sort, Service.created_at.desc()))

    # Count total for pagination
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)

    services = (await db.execute(query.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()

    # Build qs_base for pagination links
    qs_parts = []
    if q.strip():
        qs_parts.append(f"q={q.strip()}")
    if category:
        qs_parts.append(f"category={category}")
    if verified == "true":
        qs_parts.append("verified=true")
    elif status:
        qs_parts.append(f"status={status}")
    if sort and sort != "newest":
        qs_parts.append(f"sort={sort}")
    qs_base = "&".join(qs_parts)

    return templates.TemplateResponse(request, "services/_card_grid.html", {
        "services": services,
        "page": page,
        "total_pages": total_pages,
        "qs_base": qs_base,
    })


@router.get("/services/{slug}", response_class=HTMLResponse)
async def service_detail(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Service)
        .options(selectinload(Service.categories), selectinload(Service.ratings))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    return templates.TemplateResponse(request, "services/detail.html", {
        "service": service,
        "reputation_price_sats": settings.AUTH_REPUTATION_PRICE_SATS,
        "analytics_price_sats": settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS,
    })


@router.get("/submit", response_class=HTMLResponse)
async def submit_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return templates.TemplateResponse(request, "services/submit.html", {
        "categories": categories,
    })


@router.post("/submit")
@limiter.limit(RATE_SUBMIT)
async def submit_service(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    url: str = Form(...),
    description: str = Form(""),
    protocol: str = Form("L402"),
    pricing_sats: int = Form(0),
    pricing_model: str = Form("per-request"),
    owner_name: str = Form(""),
    owner_contact: str = Form(""),
    logo_url: str = Form(""),
    existing_edit_token: str = Form(""),
    x402_network: str = Form(""),
    x402_asset: str = Form(""),
    x402_pay_to: str = Form(""),
    pricing_usd: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

    # Validate category count (1–2 required)
    if len(category_ids) < 1 or len(category_ids) > 2:
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/submit.html", {
            "categories": categories,
            "error": "Select 1–2 categories.",
            "form": {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": pricing_sats,
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
            },
            "selected_category_ids": category_ids,
        }, status_code=422)

    # SECURITY: Server-side length limits prevent DB bloat and memory exhaustion.
    # HTML maxlength is client-side only and trivially bypassed. Constants in config.py.
    LENGTH_LIMITS = {
        "name": MAX_NAME, "url": MAX_URL, "description": MAX_DESCRIPTION,
        "owner_name": MAX_OWNER_NAME, "owner_contact": MAX_OWNER_CONTACT,
        "logo_url": MAX_LOGO_URL, "x402_network": MAX_X402_NETWORK,
        "x402_asset": MAX_X402_ASSET, "x402_pay_to": MAX_X402_PAY_TO,
        "pricing_usd": MAX_PRICING_USD,
    }
    for field_name, max_len in LENGTH_LIMITS.items():
        val = locals()[field_name]
        if len(val) > max_len:
            categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
            return templates.TemplateResponse(request, "services/submit.html", {
                "categories": categories,
                "error": f"{field_name} exceeds maximum length of {max_len} characters.",
                "form": {
                    "name": name, "url": url, "description": description,
                    "protocol": protocol, "pricing_sats": pricing_sats,
                    "pricing_model": pricing_model, "owner_name": owner_name,
                    "owner_contact": owner_contact, "logo_url": logo_url,
                    "existing_edit_token": existing_edit_token,
                    "x402_network": x402_network, "x402_asset": x402_asset,
                    "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
                },
                "selected_category_ids": category_ids,
            }, status_code=422)

    # SECURITY: Reject non-http(s) schemes to prevent stored XSS via javascript:/data: URIs
    parsed_url = urlparse(url)
    parsed_logo = urlparse(logo_url) if logo_url else None
    if parsed_url.scheme not in ("http", "https") or (
        parsed_logo and parsed_logo.scheme not in ("http", "https")
    ):
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/submit.html", {
            "categories": categories,
            "error": "URL and logo URL must start with http:// or https://",
            "form": {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": pricing_sats,
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
            },
            "selected_category_ids": category_ids,
        }, status_code=422)

    url = normalize_url(url)

    # Reject duplicate URLs before payment gate so users don't pay for a rejected submission
    existing = await find_existing_service(db, url)
    if existing:
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/submit.html", {
            "categories": categories,
            "error": f"A service with this URL already exists: {existing.name}",
            "form": {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": pricing_sats,
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
            },
            "selected_category_ids": category_ids,
            "existing_slug": existing.slug,
        }, status_code=409)

    # Payment gate (skipped in test mode)
    if payments_enabled():
        payment_hash = request.query_params.get("payment_hash")
        if not payment_hash:
            # Create invoice and show payment page
            invoice = await create_invoice(
                settings.AUTH_SUBMIT_PRICE_SATS, "satring.com service submission"
            )
            # Build hidden fields for resubmission
            form_fields = {
                "name": name, "url": url, "description": description,
                "protocol": protocol, "pricing_sats": str(pricing_sats),
                "pricing_model": pricing_model, "owner_name": owner_name,
                "owner_contact": owner_contact, "logo_url": logo_url,
                "existing_edit_token": existing_edit_token,
                "x402_network": x402_network, "x402_asset": x402_asset,
                "x402_pay_to": x402_pay_to, "pricing_usd": pricing_usd,
            }
            for cid in category_ids:
                form_fields[f"categories_{cid}"] = str(cid)
            return templates.TemplateResponse(request, "services/payment_required.html", {
                "payment_hash": invoice["payment_hash"],
                "payment_request": invoice["payment_request"],
                "amount_sats": settings.AUTH_SUBMIT_PRICE_SATS,
                "form_action": "/submit",
                "form_fields": form_fields,
                "category_ids": category_ids,
            })

        # Verify payment
        paid = await check_payment_status(payment_hash)
        if not paid or not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse(
                "<h1>Payment not verified</h1><p>Invoice not paid or already used.</p>",
                status_code=402,
            )

    slug = await unique_slug(db, name)

    # Fetch same-domain services (used for token reuse + auto-verify)
    domain_services = await get_same_domain_services(db, url)

    # Check if existing token matches a same-domain service
    token_reused = False
    if existing_edit_token:
        for ds in domain_services:
            if ds.edit_token_hash and verify_edit_token(existing_edit_token, ds.edit_token_hash):
                token_reused = True
                break

    if token_reused:
        edit_token = existing_edit_token
        edit_token_hash = ds.edit_token_hash
    else:
        edit_token = generate_edit_token()
        edit_token_hash = hash_token(edit_token)

    # Auto-verify if any same-domain service is already verified
    auto_verified = False
    inherited_challenge = None
    for ds in domain_services:
        if ds.domain_verified:
            auto_verified = True
            inherited_challenge = ds.domain_challenge
            break

    # Check for purged service with the same URL — overwrite instead of creating new
    purged = await find_purged_service(db, url)
    if purged:
        await overwrite_purged_service(
            db, purged,
            name=name, slug=slug, description=description,
            pricing_sats=pricing_sats, pricing_model=pricing_model,
            protocol=protocol, owner_name=owner_name, owner_contact=owner_contact,
            logo_url=logo_url, edit_token_hash=edit_token_hash,
            category_ids=category_ids,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
        )
        service = purged
    else:
        service = Service(
            name=name, slug=slug, url=url, description=description,
            protocol=protocol, pricing_sats=pricing_sats, pricing_model=pricing_model,
            owner_name=owner_name, owner_contact=owner_contact, logo_url=logo_url,
            x402_network=x402_network or None, x402_asset=x402_asset or None,
            x402_pay_to=x402_pay_to or None, pricing_usd=pricing_usd or None,
            edit_token_hash=edit_token_hash,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
        )
        if category_ids:
            cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
            service.categories = list(cats)
        db.add(service)

    await db.commit()

    # Send verification instructions if owner_contact contains an email
    email = extract_email(owner_contact or "")
    if email and not auto_verified:
        domain = extract_domain(url)
        background_tasks.add_task(send_verify_email, email, service.slug, domain)

    return templates.TemplateResponse(request, "services/submit_success.html", {
        "service": service,
        "edit_token": edit_token,
        "token_reused": token_reused,
    })


EDITABLE_FIELDS = {
    "name", "description", "pricing_sats", "pricing_model",
    "protocol", "owner_name", "owner_contact", "logo_url",
}


@router.get("/services/{slug}/edit", response_class=HTMLResponse)
async def edit_service_form(
    request: Request,
    slug: str,
    token: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    token_valid = (
        token is not None
        and service.edit_token_hash is not None
        and verify_edit_token(token, service.edit_token_hash)
    )
    token_invalid = token is not None and not token_valid
    return templates.TemplateResponse(request, "services/edit.html", {
        "service": service,
        "categories": categories,
        "token_valid": token_valid,
        "token_invalid": token_invalid,
        "token": token if token_valid else "",
    })


@router.post("/services/{slug}/edit")
@limiter.limit(RATE_EDIT)
async def edit_service(
    request: Request,
    slug: str,
    edit_token: str = Form(""),
    name: str = Form(""),
    description: str = Form(""),
    protocol: str = Form(""),
    pricing_sats: int = Form(0),
    pricing_model: str = Form(""),
    owner_name: str = Form(""),
    owner_contact: str = Form(""),
    logo_url: str = Form(""),
    x402_network: str = Form(""),
    x402_asset: str = Form(""),
    x402_pay_to: str = Form(""),
    pricing_usd: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.slug == slug)
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    if not service.edit_token_hash or not verify_edit_token(edit_token, service.edit_token_hash):
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/edit.html", {
            "service": service,
            "categories": categories,
            "token_valid": False,
            "token_invalid": True,
            "token": "",
        }, status_code=403)

    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

    # Validate category count (1–2 required)
    if len(category_ids) < 1 or len(category_ids) > 2:
        # Apply submitted values for template rendering (not committed)
        if name:
            service.name = name
        service.description = description
        service.protocol = protocol or service.protocol
        service.pricing_sats = pricing_sats
        service.pricing_model = pricing_model or service.pricing_model
        service.owner_name = owner_name
        service.owner_contact = owner_contact
        service.logo_url = logo_url
        categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
        return templates.TemplateResponse(request, "services/edit.html", {
            "service": service,
            "categories": categories,
            "token_valid": True,
            "token_invalid": False,
            "token": edit_token,
            "error": "Select 1–2 categories.",
            "selected_category_ids": category_ids,
        }, status_code=422)

    if name:
        service.name = name
    if description is not None:
        service.description = description
    if protocol:
        service.protocol = protocol
    service.pricing_sats = pricing_sats
    if pricing_model:
        service.pricing_model = pricing_model
    service.owner_name = owner_name
    service.owner_contact = owner_contact
    service.logo_url = logo_url
    service.x402_network = x402_network or None
    service.x402_asset = x402_asset or None
    service.x402_pay_to = x402_pay_to or None
    service.pricing_usd = pricing_usd or None

    cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
    service.categories = list(cats)

    await db.commit()
    return RedirectResponse(f"/services/{slug}", status_code=303)


@router.post("/services/{slug}/delete")
@limiter.limit(RATE_DELETE)
async def delete_service(
    request: Request,
    slug: str,
    edit_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    if not service.edit_token_hash or not verify_edit_token(edit_token, service.edit_token_hash):
        return HTMLResponse("Forbidden", status_code=403)

    await db.delete(service)
    await db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/services/{slug}/recover", response_class=HTMLResponse)
async def recover_form(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    challenge_active = (
        service.domain_challenge is not None
        and service.domain_challenge_expires_at is not None
        and service.domain_challenge_expires_at > datetime.now(timezone.utc).replace(tzinfo=None)
    )
    return templates.TemplateResponse(request, "services/recover.html", {
        "service": service,
        "challenge_active": challenge_active,
        "verify_path": f"{domain_root(service.url)}/.well-known/satring-verify",
    })


@router.post("/services/{slug}/recover")
@limiter.limit(RATE_RECOVER)
async def recover_service(
    request: Request,
    slug: str,
    action: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    verify_path = f"{domain_root(service.url)}/.well-known/satring-verify"

    if action == "generate":
        import secrets
        challenge = secrets.token_hex(32)
        service.domain_challenge = challenge
        service.domain_challenge_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": True,
            "verify_path": verify_path,
        })

    elif action == "verify":
        if (
            not service.domain_challenge
            or not service.domain_challenge_expires_at
            or service.domain_challenge_expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)
        ):
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": False,
                "error": "Challenge expired. Please generate a new one.",
                "verify_path": verify_path,
            })

        # SECURITY: Block SSRF — prevent server from fetching internal/private IPs
        hostname = extract_domain(service.url)
        if not hostname or not is_public_hostname(hostname):
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": "Cannot verify domain: hostname resolves to a private or unreachable address.",
                "verify_path": verify_path,
            })

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(verify_path)
            fetched = resp.text.strip()
        except Exception:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": f"Could not reach {verify_path}",
                "verify_path": verify_path,
            })

        if fetched != service.domain_challenge:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": "Challenge code does not match.",
                "verify_path": verify_path,
            })

        # Success - generate new edit token and apply to all same-domain services
        new_token = generate_edit_token()
        new_hash = hash_token(new_token)
        domain_services = await get_same_domain_services(db, service.url)
        for ds in domain_services:
            ds.edit_token_hash = new_hash
            ds.domain_verified = True
            ds.domain_challenge = service.domain_challenge
        service.edit_token_hash = new_hash
        service.domain_verified = True
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": False,
            "new_token": new_token,
            "domain_services": domain_services,
        })

    return HTMLResponse("Bad request", status_code=400)


@router.post("/services/{slug}/rate", response_class=HTMLResponse)
@limiter.limit(RATE_REVIEW)
async def rate_service(
    request: Request,
    slug: str,
    score: int = Form(...),
    comment: str = Form(""),
    reviewer_name: str = Form("Anonymous"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("Not Found", status_code=404)

    if score < 1:
        score = 1
    if score > 5:
        score = 5

    # SECURITY: Server-side length limits (HTML maxlength is trivially bypassed)
    if len(reviewer_name) > MAX_REVIEWER_NAME or len(comment) > MAX_COMMENT:
        return HTMLResponse("Input exceeds maximum length.", status_code=422)

    # Payment gate (skipped in test mode)
    if payments_enabled():
        payment_hash = request.query_params.get("payment_hash")
        if not payment_hash:
            invoice = await create_invoice(
                settings.AUTH_REVIEW_PRICE_SATS, "satring.com review submission"
            )
            form_fields = {
                "score": str(score),
                "comment": comment,
                "reviewer_name": reviewer_name,
            }
            html = templates.TemplateResponse(request, "services/_payment_widget.html", {
                "payment_hash": invoice["payment_hash"],
                "payment_request": invoice["payment_request"],
                "amount_sats": settings.AUTH_REVIEW_PRICE_SATS,
                "form_action": f"/services/{slug}/rate",
                "form_fields": form_fields,
                "htmx_mode": True,
                "slug": slug,
            })
            html.headers["HX-Retarget"] = "#payment-area"
            html.headers["HX-Reswap"] = "innerHTML"
            return html

        paid = await check_payment_status(payment_hash)
        if not paid or not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse(
                '<div class="text-red-400 text-sm">Payment not verified or already used.</div>',
                status_code=402,
            )

    rating = Rating(
        service_id=service.id,
        score=score,
        comment=comment,
        reviewer_name=reviewer_name or "Anonymous",
    )
    db.add(rating)
    await db.flush()

    avg_result = await db.execute(
        select(func.avg(Rating.score), func.count(Rating.id))
        .where(Rating.service_id == service.id)
    )
    avg_row = avg_result.one()
    service.avg_rating = round(float(avg_row[0]), 1)
    service.rating_count = avg_row[1]
    await db.commit()

    return templates.TemplateResponse(request, "services/_review_bubble.html", {
        "rating": rating,
    })


@router.get("/services/{slug}/reputation-invoice", response_class=HTMLResponse)
async def reputation_invoice(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    # Verify service exists
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    if not result.scalars().first():
        return HTMLResponse("Not Found", status_code=404)

    if not payments_enabled():
        # Test mode: skip invoice, go straight to result
        data = await build_reputation_data(db, slug)
        return templates.TemplateResponse(request, "services/_reputation_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_REPUTATION_PRICE_SATS, "satring.com reputation report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_REPUTATION_PRICE_SATS,
        "result_url": f"/services/{slug}/reputation-result",
        "target_id": "reputation-area",
        "label": "reputation report",
    })


@router.get("/services/{slug}/reputation-result", response_class=HTMLResponse)
async def reputation_result(request: Request, slug: str, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid = await check_payment_status(payment_hash)
        if not paid or not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment not verified or already used.", status_code=402)

    data = await build_reputation_data(db, slug)
    return templates.TemplateResponse(request, "services/_reputation_result.html", {"data": data})


@router.get("/services/{slug}/analytics-invoice", response_class=HTMLResponse)
async def service_analytics_invoice(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    # Verify service exists
    result = await db.execute(
        select(Service).where(Service.slug == slug).where(Service.status != "purged")
    )
    if not result.scalars().first():
        return HTMLResponse("Not Found", status_code=404)

    if not payments_enabled():
        data = await build_service_analytics(db, slug)
        return templates.TemplateResponse(request, "services/_service_analytics_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS, "satring.com service health report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_SERVICE_ANALYTICS_PRICE_SATS,
        "result_url": f"/services/{slug}/analytics-result",
        "target_id": "analytics-area",
        "label": "health report",
    })


@router.get("/services/{slug}/analytics-result", response_class=HTMLResponse)
async def service_analytics_result(request: Request, slug: str, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid = await check_payment_status(payment_hash)
        if not paid or not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment not verified or already used.", status_code=402)

    data = await build_service_analytics(db, slug)
    return templates.TemplateResponse(request, "services/_service_analytics_result.html", {"data": data})


@router.get("/analytics-invoice", response_class=HTMLResponse)
async def analytics_invoice(request: Request, db: AsyncSession = Depends(get_db)):
    if not payments_enabled():
        data = await build_analytics_data(db)
        return templates.TemplateResponse(request, "services/_analytics_result.html", {"data": data})

    invoice = await create_invoice(
        settings.AUTH_ANALYTICS_PRICE_SATS, "satring.com analytics report"
    )
    return templates.TemplateResponse(request, "services/_paid_report_widget.html", {
        "payment_hash": invoice["payment_hash"],
        "payment_request": invoice["payment_request"],
        "amount_sats": settings.AUTH_ANALYTICS_PRICE_SATS,
        "result_url": "/analytics-result",
        "target_id": "analytics-area",
        "label": "directory analytics",
    })


@router.get("/analytics-result", response_class=HTMLResponse)
async def analytics_result(request: Request, payment_hash: str = "", db: AsyncSession = Depends(get_db)):
    if payments_enabled():
        if not payment_hash:
            return HTMLResponse("Payment required", status_code=402)
        paid = await check_payment_status(payment_hash)
        if not paid or not await check_and_consume_payment(payment_hash, db):
            return HTMLResponse("Payment not verified or already used.", status_code=402)

    data = await build_analytics_data(db)
    return templates.TemplateResponse(request, "services/_analytics_result.html", {"data": data})


@router.get("/payment-status/{payment_hash}")
@limiter.limit(RATE_PAYMENT_STATUS)
async def payment_status(request: Request, payment_hash: str):
    if not payments_enabled():
        return JSONResponse({"paid": True})
    paid = await check_payment_status(payment_hash)
    return JSONResponse({"paid": paid})


@router.get("/sitemap.xml")
@limiter.limit(RATE_SITEMAP)
async def sitemap(request: Request, db: AsyncSession = Depends(get_db)):
    from fastapi.responses import Response
    base = settings.BASE_URL.rstrip("/")

    urls = [
        f'  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>',
        f'  <url><loc>{base}/submit</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>',
        f'  <url><loc>{base}/docs</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>',
        f'  <url><loc>{base}/robots.txt</loc><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        f'  <url><loc>{base}/llms.txt</loc><changefreq>weekly</changefreq><priority>0.4</priority></url>',
    ]

    result = await db.execute(select(Service.slug).where(Service.status != "purged"))
    for (slug,) in result.all():
        urls.append(f'  <url><loc>{base}/services/{slug}</loc><changefreq>daily</changefreq><priority>0.7</priority></url>')

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    xml += '\n'.join(urls)
    xml += '\n</urlset>'
    return Response(content=xml, media_type="application/xml")


@router.get("/robots.txt")
async def robots_txt():
    base = settings.BASE_URL.rstrip("/")
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        "User-agent: AhrefsBot\n"
        "Disallow: /\n"
        "\n"
        "User-agent: SemrushBot\n"
        "Disallow: /\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
        f"Llms-txt: {base}/llms.txt\n"
    )
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content)


@router.get("/llms.txt")
async def llms_txt():
    from fastapi.responses import PlainTextResponse
    from app.main import SEED_CATEGORIES
    base = settings.BASE_URL.rstrip("/")

    lines = [
        "# satring",
        "",
        "> L402 + x402 paid API directory. Discover Lightning and USDC paywalled APIs for AI agents and developers.",
        "",
        "Satring is a curated directory of L402 and x402 services: APIs that accept Bitcoin Lightning or USDC micropayments.",
        "AI agents use satring to find and connect to paid APIs autonomously via MCP.",
        "Humans use it to discover, rate, and submit services.",
        "",
        f"- [Browse directory]({base}/): Search and filter services by category, status, rating",
        f"- [Submit a service]({base}/submit): List your L402 API (Lightning payment required)",
        f"- [API docs]({base}/docs): OpenAPI/Swagger interactive documentation",
        f"- [JSON API]({base}/api/v1/services): Programmatic access to the full catalog",
        "",
        "## API",
        "",
        f"- [List services]({base}/api/v1/services): GET — paginated, filterable by category. Returns name, url, pricing, protocol, ratings, categories.",
        f"- [Search]({base}/api/v1/search?q=example): GET — full-text search across names and descriptions",
        f"- [Service detail]({base}/api/v1/services/{{slug}}): GET — full service info with categories",
        f"- [Ratings]({base}/api/v1/services/{{slug}}/ratings): GET — paginated reviews for a service",
        f"- [Submit service]({base}/api/v1/services): POST — create a new listing (L402 payment required)",
        f"- [Submit rating]({base}/api/v1/services/{{slug}}/ratings): POST — rate a service (L402 payment required)",
        f"- [Bulk export]({base}/api/v1/services/bulk): GET — all services as JSON (L402 payment required)",
        f"- [Analytics]({base}/api/v1/analytics): GET — aggregate directory stats (L402 payment required)",
        "",
        "## Categories",
        "",
    ]
    for name, slug, description in SEED_CATEGORIES:
        lines.append(f"- [{name}]({base}/?category={slug}): {description}")

    lines.append("")
    return PlainTextResponse("\n".join(lines), media_type="text/plain")
