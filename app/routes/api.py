from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import get_db
from app.l402 import require_l402
from app.models import Service, Category, Rating, service_categories

router = APIRouter(tags=["API"])


# --- Pydantic Schemas ---

class CategoryOut(BaseModel):
    id: int
    name: str
    slug: str
    description: str

    model_config = {"from_attributes": True}


class RatingOut(BaseModel):
    id: int
    score: int
    comment: str
    reviewer_name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class RatingCreate(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str = ""
    reviewer_name: str = "Anonymous"


class ServiceOut(BaseModel):
    id: int
    name: str
    slug: str
    url: str
    description: str
    pricing_sats: int
    pricing_model: str
    protocol: str
    owner_name: str
    logo_url: str
    avg_rating: float
    rating_count: int
    categories: list[CategoryOut]
    created_at: datetime

    model_config = {"from_attributes": True}


class ServiceListOut(BaseModel):
    services: list[ServiceOut]
    total: int
    page: int
    page_size: int


class ServiceCreate(BaseModel):
    name: str
    url: HttpUrl
    description: str = ""
    pricing_sats: int = 0
    pricing_model: str = "per-request"
    protocol: str = "L402"
    owner_name: str = ""
    owner_contact: str = ""
    logo_url: str = ""
    category_ids: list[int] = []


# --- Helpers ---

async def paginated_services(db: AsyncSession, query, page: int, page_size: int) -> ServiceListOut:
    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    offset = (page - 1) * page_size
    results = await db.execute(
        query.options(selectinload(Service.categories))
        .offset(offset).limit(page_size)
    )
    services = results.scalars().all()
    return ServiceListOut(
        services=[ServiceOut.model_validate(s) for s in services],
        total=total, page=page, page_size=page_size,
    )


async def get_service_or_404(db: AsyncSession, slug: str) -> Service:
    result = await db.execute(
        select(Service)
        .options(selectinload(Service.categories))
        .where(Service.slug == slug)
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


# --- Free Endpoints ---

# IMPORTANT: /services/bulk BEFORE /services/{slug}
@router.get("/services/bulk", response_model=list[ServiceOut])
async def bulk_export(request: Request, db: AsyncSession = Depends(get_db)):
    await require_l402(request=request)
    result = await db.execute(
        select(Service).options(selectinload(Service.categories)).order_by(Service.id)
    )
    return [ServiceOut.model_validate(s) for s in result.scalars().all()]


@router.get("/services", response_model=ServiceListOut)
async def list_services(
    category: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).order_by(Service.created_at.desc())
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    return await paginated_services(db, query, page, page_size)


@router.get("/services/{slug}", response_model=ServiceOut)
async def get_service(slug: str, db: AsyncSession = Depends(get_db)):
    return ServiceOut.model_validate(await get_service_or_404(db, slug))


@router.post("/services", response_model=ServiceOut, status_code=201)
async def create_service(request: Request, body: ServiceCreate, db: AsyncSession = Depends(get_db)):
    await require_l402(request=request, amount_sats=settings.AUTH_SUBMIT_PRICE_SATS, memo="Satring service submission")
    from app.utils import unique_slug
    slug = await unique_slug(db, body.name)
    service = Service(
        name=body.name, slug=slug, url=str(body.url), description=body.description,
        pricing_sats=body.pricing_sats, pricing_model=body.pricing_model,
        protocol=body.protocol, owner_name=body.owner_name,
        owner_contact=body.owner_contact, logo_url=body.logo_url,
    )
    if body.category_ids:
        cats = (await db.execute(
            select(Category).where(Category.id.in_(body.category_ids))
        )).scalars().all()
        service.categories = list(cats)

    db.add(service)
    await db.commit()
    await db.refresh(service)
    result = await db.execute(
        select(Service).options(selectinload(Service.categories)).where(Service.id == service.id)
    )
    return ServiceOut.model_validate(result.scalars().first())


@router.get("/search", response_model=ServiceListOut)
async def search_services(
    q: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).order_by(Service.created_at.desc())
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(Service.name.ilike(pattern) | Service.description.ilike(pattern))
    return await paginated_services(db, query, page, page_size)


@router.get("/services/{slug}/ratings", response_model=list[RatingOut])
async def list_ratings(slug: str, db: AsyncSession = Depends(get_db)):
    service = await get_service_or_404(db, slug)
    result = await db.execute(
        select(Rating).where(Rating.service_id == service.id).order_by(Rating.created_at.desc())
    )
    return [RatingOut.model_validate(r) for r in result.scalars().all()]


@router.post("/services/{slug}/ratings", response_model=RatingOut, status_code=201)
async def create_rating(request: Request, slug: str, body: RatingCreate, db: AsyncSession = Depends(get_db)):
    await require_l402(request=request, amount_sats=settings.AUTH_REVIEW_PRICE_SATS, memo="Satring review submission")
    service = await get_service_or_404(db, slug)
    rating = Rating(
        service_id=service.id,
        score=body.score,
        comment=body.comment,
        reviewer_name=body.reviewer_name or "Anonymous",
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
    return RatingOut.model_validate(rating)


# --- Premium Endpoints (L402-gated) ---

@router.get("/analytics")
async def analytics(request: Request, db: AsyncSession = Depends(get_db)):
    await require_l402(request=request)

    total_services = (await db.execute(select(func.count(Service.id)))).scalar() or 0
    total_ratings = (await db.execute(select(func.count(Rating.id)))).scalar() or 0
    avg_price = (await db.execute(select(func.avg(Service.pricing_sats)))).scalar() or 0

    top_rated = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.rating_count >= 1)
        .order_by(Service.avg_rating.desc()).limit(10)
    )

    return {
        "total_services": total_services,
        "total_ratings": total_ratings,
        "avg_price_sats": round(float(avg_price), 1),
        "top_rated": [ServiceOut.model_validate(s) for s in top_rated.scalars().all()],
    }


@router.get("/services/{slug}/reputation")
async def reputation(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    await require_l402(request=request)

    service = await get_service_or_404(db, slug)
    dist_result = await db.execute(
        select(Rating.score, func.count(Rating.id))
        .where(Rating.service_id == service.id)
        .group_by(Rating.score)
    )
    distribution = {row[0]: row[1] for row in dist_result.all()}

    recent = await db.execute(
        select(Rating).where(Rating.service_id == service.id)
        .order_by(Rating.created_at.desc()).limit(20)
    )

    return {
        "service": service.name,
        "slug": service.slug,
        "avg_rating": service.avg_rating,
        "rating_count": service.rating_count,
        "distribution": {str(i): distribution.get(i, 0) for i in range(1, 6)},
        "recent_reviews": [RatingOut.model_validate(r) for r in recent.scalars().all()],
    }
