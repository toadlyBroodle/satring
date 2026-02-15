from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.l402 import create_invoice, check_payment_status, check_and_consume_payment
from app.main import templates
from app.models import Service, Category, Rating, service_categories
from app.utils import unique_slug, generate_edit_token, hash_token, verify_edit_token

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def directory(
    request: Request,
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()

    query = select(Service).options(selectinload(Service.categories))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
    }
    query = query.order_by(sort_map.get(sort, Service.created_at.desc()))

    services = (await db.execute(query)).scalars().all()
    return templates.TemplateResponse(request, "services/list.html", {
        "services": services,
        "categories": categories,
        "active_category": category,
        "active_status": status,
        "active_sort": sort,
    })


@router.get("/search", response_class=HTMLResponse)
async def search(
    request: Request,
    q: str = "",
    category: str | None = None,
    status: str | None = None,
    sort: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).options(selectinload(Service.categories))
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(Service.name.ilike(pattern) | Service.description.ilike(pattern))
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status:
        query = query.where(Service.status == status)

    sort_map = {
        "top-rated": Service.avg_rating.desc(),
        "cheapest": Service.pricing_sats.asc(),
        "most-reviewed": Service.rating_count.desc(),
    }
    query = query.order_by(sort_map.get(sort, Service.created_at.desc()))

    services = (await db.execute(query)).scalars().all()
    return templates.TemplateResponse(request, "services/_card_grid.html", {
        "services": services,
    })


@router.get("/services/{slug}", response_class=HTMLResponse)
async def service_detail(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Service)
        .options(selectinload(Service.categories), selectinload(Service.ratings))
        .where(Service.slug == slug)
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    return templates.TemplateResponse(request, "services/detail.html", {
        "service": service,
    })


@router.get("/submit", response_class=HTMLResponse)
async def submit_form(request: Request, db: AsyncSession = Depends(get_db)):
    categories = (await db.execute(select(Category).order_by(Category.name))).scalars().all()
    return templates.TemplateResponse(request, "services/submit.html", {
        "categories": categories,
    })


@router.post("/submit")
async def submit_service(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    description: str = Form(""),
    protocol: str = Form("L402"),
    pricing_sats: int = Form(0),
    pricing_model: str = Form("per-request"),
    owner_name: str = Form(""),
    owner_contact: str = Form(""),
    logo_url: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

    # Payment gate (skipped in test mode)
    if settings.AUTH_ROOT_KEY != "test-mode":
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
        if not paid or not check_and_consume_payment(payment_hash):
            return HTMLResponse(
                "<h1>Payment not verified</h1><p>Invoice not paid or already used.</p>",
                status_code=402,
            )

    slug = await unique_slug(db, name)
    edit_token = generate_edit_token()
    service = Service(
        name=name, slug=slug, url=url, description=description,
        protocol=protocol, pricing_sats=pricing_sats, pricing_model=pricing_model,
        owner_name=owner_name, owner_contact=owner_contact, logo_url=logo_url,
        edit_token_hash=hash_token(edit_token),
    )
    if category_ids:
        cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
        service.categories = list(cats)

    db.add(service)
    await db.commit()
    return templates.TemplateResponse(request, "services/submit_success.html", {
        "service": service,
        "edit_token": edit_token,
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
        select(Service).options(selectinload(Service.categories)).where(Service.slug == slug)
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
    return templates.TemplateResponse(request, "services/edit.html", {
        "service": service,
        "categories": categories,
        "token_valid": token_valid,
        "token": token if token_valid else "",
    })


@router.post("/services/{slug}/edit")
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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Service).options(selectinload(Service.categories)).where(Service.slug == slug)
    )
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)
    if not service.edit_token_hash or not verify_edit_token(edit_token, service.edit_token_hash):
        return HTMLResponse("<h1>Forbidden</h1><p>Invalid edit token.</p>", status_code=403)

    form_data = await request.form()
    category_ids = [int(v) for k, v in form_data.multi_items() if k == "categories"]

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

    cats = (await db.execute(select(Category).where(Category.id.in_(category_ids)))).scalars().all()
    service.categories = list(cats)

    await db.commit()
    return RedirectResponse(f"/services/{slug}", status_code=303)


@router.get("/services/{slug}/recover", response_class=HTMLResponse)
async def recover_form(
    request: Request,
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Service).where(Service.slug == slug))
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    challenge_active = (
        service.domain_challenge is not None
        and service.domain_challenge_expires_at is not None
        and service.domain_challenge_expires_at > datetime.now(timezone.utc)
    )
    return templates.TemplateResponse(request, "services/recover.html", {
        "service": service,
        "challenge_active": challenge_active,
    })


@router.post("/services/{slug}/recover")
async def recover_service(
    request: Request,
    slug: str,
    action: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Service).where(Service.slug == slug))
    service = result.scalars().first()
    if not service:
        return HTMLResponse("<h1>Not Found</h1>", status_code=404)

    if action == "generate":
        import secrets
        challenge = secrets.token_hex(32)
        service.domain_challenge = challenge
        service.domain_challenge_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": True,
        })

    elif action == "verify":
        if (
            not service.domain_challenge
            or not service.domain_challenge_expires_at
            or service.domain_challenge_expires_at <= datetime.now(timezone.utc)
        ):
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": False,
                "error": "Challenge expired. Please generate a new one.",
            })

        verify_url = f"{service.url.rstrip('/')}/.well-known/satring-verify"
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                resp = await http.get(verify_url)
            fetched = resp.text.strip()
        except Exception:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": f"Could not reach {verify_url}",
            })

        if fetched != service.domain_challenge:
            return templates.TemplateResponse(request, "services/recover.html", {
                "service": service,
                "challenge_active": True,
                "error": "Challenge code does not match.",
            })

        # Success - generate new edit token
        new_token = generate_edit_token()
        service.edit_token_hash = hash_token(new_token)
        service.domain_challenge = None
        service.domain_challenge_expires_at = None
        await db.commit()
        return templates.TemplateResponse(request, "services/recover.html", {
            "service": service,
            "challenge_active": False,
            "new_token": new_token,
        })

    return HTMLResponse("Bad request", status_code=400)


@router.post("/services/{slug}/rate", response_class=HTMLResponse)
async def rate_service(
    request: Request,
    slug: str,
    score: int = Form(...),
    comment: str = Form(""),
    reviewer_name: str = Form("Anonymous"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Service).where(Service.slug == slug))
    service = result.scalars().first()
    if not service:
        return HTMLResponse("Not Found", status_code=404)

    if score < 1:
        score = 1
    if score > 5:
        score = 5

    # Payment gate (skipped in test mode)
    if settings.AUTH_ROOT_KEY != "test-mode":
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
        if not paid or not check_and_consume_payment(payment_hash):
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


@router.get("/payment-status/{payment_hash}")
async def payment_status(payment_hash: str):
    if settings.AUTH_ROOT_KEY == "test-mode":
        return JSONResponse({"paid": True})
    paid = await check_payment_status(payment_hash)
    return JSONResponse({"paid": paid})
