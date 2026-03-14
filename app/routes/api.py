import json
import math
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator
from sqlalchemy import case, select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import (
    settings, MAX_NAME, MAX_URL, MAX_DESCRIPTION, MAX_OWNER_NAME,
    MAX_OWNER_CONTACT, MAX_LOGO_URL, MAX_REVIEWER_NAME, MAX_COMMENT, MAX_PRICING_SATS,
    MAX_X402_NETWORK, MAX_X402_ASSET, MAX_X402_PAY_TO, MAX_PRICING_USD,
    RATE_SUBMIT, RATE_EDIT, RATE_DELETE, RATE_RECOVER, RATE_REVIEW, RATE_SEARCH_API,
    RATE_LIST_API, RATE_DETAIL_API,
)
from app.database import get_db
from app.payment import require_payment
from app.main import limiter
from app.models import Service, Category, Rating, RouteUsage, service_categories
from app.utils import generate_edit_token, hash_token, verify_edit_token, get_same_domain_services, domain_root, extract_domain, is_public_hostname, extract_email, send_verify_email, find_purged_service, find_existing_service, normalize_url, overwrite_purged_service, escape_like

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
    # SECURITY: max_length limits prevent DB bloat and memory exhaustion (constants in config.py)
    comment: str = Field(default="", max_length=MAX_COMMENT)
    reviewer_name: str = Field(default="Anonymous", max_length=MAX_REVIEWER_NAME)


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
    x402_network: str | None = None
    x402_asset: str | None = None
    x402_pay_to: str | None = None
    pricing_usd: str | None = None
    avg_rating: float
    rating_count: int
    domain_verified: bool
    categories: list[CategoryOut]
    created_at: datetime

    model_config = {"from_attributes": True}


class ServiceListOut(BaseModel):
    services: list[ServiceOut]
    total: int
    page: int
    page_size: int


class ServiceCreate(BaseModel):
    # SECURITY: max_length limits prevent DB bloat and memory exhaustion (constants in config.py)
    name: str = Field(min_length=1, max_length=MAX_NAME)
    url: HttpUrl
    description: str = Field(default="", max_length=MAX_DESCRIPTION)
    pricing_sats: int = Field(default=0, ge=0, le=MAX_PRICING_SATS)
    pricing_model: str = Field(default="per-request", max_length=50)
    protocol: str = Field(default="L402", max_length=10)
    owner_name: str = Field(default="", max_length=MAX_OWNER_NAME)
    owner_contact: str = Field(default="", max_length=MAX_OWNER_CONTACT)
    logo_url: str = Field(default="", max_length=MAX_LOGO_URL)
    x402_network: str | None = Field(default=None, max_length=MAX_X402_NETWORK)
    x402_asset: str | None = Field(default=None, max_length=MAX_X402_ASSET)
    x402_pay_to: str | None = Field(default=None, max_length=MAX_X402_PAY_TO)
    pricing_usd: str | None = Field(default=None, max_length=MAX_PRICING_USD)
    category_ids: list[int] = Field(default_factory=lambda: [], description="1–2 category IDs required")
    existing_edit_token: str | None = None

    # SECURITY: Reject non-http(s) schemes to prevent stored XSS via javascript:/data: URIs
    @field_validator("logo_url")
    @classmethod
    def check_logo_url_scheme(cls, v: str) -> str:
        if v and urlparse(v).scheme not in ("http", "https"):
            raise ValueError("logo_url must start with http:// or https://")
        return v

    @field_validator("category_ids")
    @classmethod
    def check_category_count(cls, v: list[int]) -> list[int]:
        if len(v) < 1 or len(v) > 2:
            raise ValueError("Select 1–2 categories")
        return v

    @model_validator(mode="after")
    def check_x402_requires_fields(self):
        if self.protocol == "X402":
            if not self.x402_pay_to:
                raise ValueError("x402_pay_to is required when protocol is X402")
            if not self.x402_network:
                raise ValueError("x402_network is required when protocol is X402")
        return self


class ServiceCreateOut(ServiceOut):
    edit_token: str | None = None
    token_reused: bool = False


class ServiceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    pricing_sats: int | None = None
    pricing_model: str | None = None
    protocol: str | None = None
    owner_name: str | None = None
    owner_contact: str | None = None
    logo_url: str | None = None
    x402_network: str | None = None
    x402_asset: str | None = None
    x402_pay_to: str | None = None
    pricing_usd: str | None = None
    category_ids: list[int] | None = None

    @field_validator("category_ids")
    @classmethod
    def check_category_count(cls, v: list[int] | None) -> list[int] | None:
        if v is not None and (len(v) < 1 or len(v) > 2):
            raise ValueError("Select 1–2 categories")
        return v


# --- Analytics Schemas ---


class LeaderboardEntry(BaseModel):
    name: str
    slug: str
    avg_rating: float
    rating_count: int
    pricing_sats: int

    model_config = {"from_attributes": True}


class CategoryStats(BaseModel):
    name: str
    slug: str
    service_count: int
    avg_rating: float
    avg_price_sats: float
    live_count: int


class HealthOverview(BaseModel):
    by_status: dict[str, int]
    live_percentage: float
    domain_verified_count: int
    domain_verified_percentage: float


class PricingStats(BaseModel):
    avg_sats: float
    median_sats: float
    min_sats: int
    max_sats: int
    free_count: int
    by_model: dict[str, int]
    by_protocol: dict[str, int]


class GrowthStats(BaseModel):
    services_added_last_7d: int
    services_added_last_30d: int
    ratings_added_last_7d: int
    ratings_added_last_30d: int
    newest_service: dict | None


class RouteHitStats(BaseModel):
    route: str
    total_hits: int
    unique_ips: int


class UsageTimeBucket(BaseModel):
    period: str       # ISO hour or date string
    total_hits: int


class SourceHits(BaseModel):
    total_24h: int
    total_7d: int
    total_30d: int
    unique_ips_24h: int
    unique_ips_7d: int
    unique_ips_30d: int
    top_routes_30d: list[RouteHitStats]
    hourly_24h: list[UsageTimeBucket]
    daily_30d: list[UsageTimeBucket]


class UsageStats(BaseModel):
    api: SourceHits
    web: SourceHits


class AnalyticsResponse(BaseModel):
    generated_at: str
    total_services: int
    total_ratings: int
    total_categories: int
    health: HealthOverview
    pricing: PricingStats
    categories: list[CategoryStats]
    growth: GrowthStats
    top_rated: list[LeaderboardEntry]
    most_reviewed: list[LeaderboardEntry]
    recently_added: list[LeaderboardEntry]
    usage: UsageStats | None = None


# --- Reputation Schemas ---


class ServiceDetail(BaseModel):
    name: str
    slug: str
    url: str
    description: str
    pricing_sats: int
    pricing_model: str
    protocol: str
    owner_name: str
    logo_url: str
    x402_network: str | None = None
    x402_asset: str | None = None
    x402_pay_to: str | None = None
    pricing_usd: str | None = None
    domain_verified: bool
    status: str
    last_probed_at: datetime | None
    dead_since: datetime | None
    categories: list[CategoryOut]
    created_at: datetime
    age_days: int


class RatingSummary(BaseModel):
    avg_rating: float
    rating_count: int
    distribution: dict[str, int]
    distribution_pct: dict[str, float]
    std_deviation: float
    sentiment_label: str


class MonthlyTrend(BaseModel):
    month: str
    count: int
    avg_score: float


class PeerEntry(BaseModel):
    name: str
    slug: str
    avg_rating: float
    rating_count: int


class PeerComparison(BaseModel):
    category_avg_rating: float
    category_avg_price_sats: float
    category_total_services: int
    rating_rank: int
    rating_percentile: float
    price_rank: int
    review_volume_rank: int
    peers_rated_higher: list[PeerEntry]
    peers_rated_lower: list[PeerEntry]


class ReviewActivity(BaseModel):
    first_review_at: datetime | None
    latest_review_at: datetime | None
    days_since_last_review: int | None
    unique_reviewers: int
    anonymous_count: int
    avg_comment_length: float
    reviews_with_comments: int
    reviews_without_comments: int


class ReputationResponse(BaseModel):
    generated_at: str
    service: ServiceDetail
    rating_summary: RatingSummary
    rating_trend: list[MonthlyTrend]
    peer_comparison: PeerComparison | None
    review_activity: ReviewActivity
    recent_reviews: list[RatingOut]


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
        .where(Service.status != "purged")
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


def compute_median(values: list) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return float(s[mid])


def std_deviation_from_dist(distribution: dict[int, int]) -> float:
    total = sum(distribution.values())
    if total == 0:
        return 0.0
    mean = sum(k * v for k, v in distribution.items()) / total
    variance = sum(v * (k - mean) ** 2 for k, v in distribution.items()) / total
    return round(math.sqrt(variance), 2)


def sentiment_label(avg: float, count: int) -> str:
    if count == 0:
        return "no_reviews"
    if avg >= 4.5:
        return "very_positive"
    if avg >= 3.5:
        return "positive"
    if avg >= 2.5:
        return "mixed"
    if avg >= 1.5:
        return "negative"
    return "very_negative"


# --- Shared data builders (used by both API and web routes) ---


async def build_analytics_data(db: AsyncSession) -> AnalyticsResponse:
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # --- Totals ---
    total_services = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged")
    )).scalar() or 0
    total_ratings = (await db.execute(
        select(func.count(Rating.id)).join(Service).where(Service.status != "purged")
    )).scalar() or 0
    total_categories = (await db.execute(
        select(func.count(Category.id))
    )).scalar() or 0

    # --- Health ---
    status_rows = (await db.execute(
        select(Service.status, func.count(Service.id))
        .where(Service.status != "purged")
        .group_by(Service.status)
    )).all()
    by_status = {row[0]: row[1] for row in status_rows}
    live_pct = round(by_status.get("live", 0) / total_services * 100, 1) if total_services else 0.0

    domain_verified_count = (await db.execute(
        select(func.count(Service.id))
        .where(Service.status != "purged")
        .where(Service.domain_verified == True)
    )).scalar() or 0
    domain_verified_pct = round(domain_verified_count / total_services * 100, 1) if total_services else 0.0

    # --- Pricing ---
    pricing_agg = (await db.execute(
        select(
            func.avg(Service.pricing_sats),
            func.min(Service.pricing_sats),
            func.max(Service.pricing_sats),
        ).where(Service.status != "purged")
    )).one()
    all_prices = (await db.execute(
        select(Service.pricing_sats).where(Service.status != "purged").order_by(Service.pricing_sats)
    )).scalars().all()
    free_count = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged").where(Service.pricing_sats == 0)
    )).scalar() or 0
    by_model_rows = (await db.execute(
        select(Service.pricing_model, func.count(Service.id))
        .where(Service.status != "purged")
        .group_by(Service.pricing_model)
    )).all()
    by_protocol_rows = (await db.execute(
        select(Service.protocol, func.count(Service.id))
        .where(Service.status != "purged")
        .group_by(Service.protocol)
    )).all()

    # --- Categories ---
    cat_rows = (await db.execute(
        select(
            Category.name,
            Category.slug,
            func.count(Service.id),
            func.coalesce(func.avg(Service.avg_rating), 0.0),
            func.coalesce(func.avg(Service.pricing_sats), 0.0),
            func.sum(case((Service.status == "live", 1), else_=0)),
        )
        .join(service_categories, Category.id == service_categories.c.category_id)
        .join(Service, Service.id == service_categories.c.service_id)
        .where(Service.status != "purged")
        .group_by(Category.id)
        .order_by(func.count(Service.id).desc())
    )).all()

    # --- Growth ---
    seven_ago = now - timedelta(days=7)
    thirty_ago = now - timedelta(days=30)
    svc_7d = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged").where(Service.created_at >= seven_ago)
    )).scalar() or 0
    svc_30d = (await db.execute(
        select(func.count(Service.id)).where(Service.status != "purged").where(Service.created_at >= thirty_ago)
    )).scalar() or 0
    rat_7d = (await db.execute(
        select(func.count(Rating.id)).join(Service).where(Service.status != "purged").where(Rating.created_at >= seven_ago)
    )).scalar() or 0
    rat_30d = (await db.execute(
        select(func.count(Rating.id)).join(Service).where(Service.status != "purged").where(Rating.created_at >= thirty_ago)
    )).scalar() or 0
    newest_row = (await db.execute(
        select(Service.name, Service.slug, Service.created_at)
        .where(Service.status != "purged")
        .order_by(Service.created_at.desc()).limit(1)
    )).first()

    # --- Leaderboards ---
    top_rated_rows = (await db.execute(
        select(Service).where(Service.status != "purged")
        .where(Service.rating_count >= 3)
        .order_by(Service.avg_rating.desc(), Service.rating_count.desc()).limit(10)
    )).scalars().all()
    most_reviewed_rows = (await db.execute(
        select(Service).where(Service.status != "purged")
        .where(Service.rating_count >= 1)
        .order_by(Service.rating_count.desc(), Service.avg_rating.desc()).limit(10)
    )).scalars().all()
    recently_added_rows = (await db.execute(
        select(Service).where(Service.status != "purged")
        .order_by(Service.created_at.desc()).limit(10)
    )).scalars().all()

    def _lb(s: Service) -> LeaderboardEntry:
        return LeaderboardEntry(
            name=s.name, slug=s.slug, avg_rating=s.avg_rating,
            rating_count=s.rating_count, pricing_sats=s.pricing_sats,
        )

    # --- Route Usage ---
    async def _source_hits(source: str) -> SourceHits:
        now_24h = now - timedelta(hours=24)
        now_7d = now - timedelta(days=7)

        # Totals and unique IPs by time window
        def _totals_query(since):
            return select(
                func.coalesce(func.sum(RouteUsage.hit_count), 0),
                func.coalesce(func.sum(RouteUsage.unique_ips), 0),
            ).where(
                RouteUsage.source == source,
                RouteUsage.hour >= since,
            )

        row_24h = (await db.execute(_totals_query(now_24h))).one()
        row_7d = (await db.execute(_totals_query(now_7d))).one()
        row_30d = (await db.execute(_totals_query(thirty_ago))).one()

        # Top 10 routes (30d)
        top_rows = (await db.execute(
            select(
                RouteUsage.route,
                func.sum(RouteUsage.hit_count).label("total"),
                func.sum(RouteUsage.unique_ips).label("ips"),
            )
            .where(RouteUsage.source == source)
            .where(RouteUsage.hour >= thirty_ago)
            .group_by(RouteUsage.route)
            .order_by(func.sum(RouteUsage.hit_count).desc())
            .limit(10)
        )).all()

        # Hourly hits (24h)
        hourly_rows = (await db.execute(
            select(
                RouteUsage.hour,
                func.sum(RouteUsage.hit_count).label("total"),
            )
            .where(RouteUsage.source == source)
            .where(RouteUsage.hour >= now_24h)
            .group_by(RouteUsage.hour)
            .order_by(RouteUsage.hour)
        )).all()

        # Daily hits (30d) using SQLite date()
        daily_rows = (await db.execute(
            select(
                func.date(RouteUsage.hour).label("day"),
                func.sum(RouteUsage.hit_count).label("total"),
            )
            .where(RouteUsage.source == source)
            .where(RouteUsage.hour >= thirty_ago)
            .group_by(func.date(RouteUsage.hour))
            .order_by(func.date(RouteUsage.hour))
        )).all()

        return SourceHits(
            total_24h=int(row_24h[0]),
            total_7d=int(row_7d[0]),
            total_30d=int(row_30d[0]),
            unique_ips_24h=int(row_24h[1]),
            unique_ips_7d=int(row_7d[1]),
            unique_ips_30d=int(row_30d[1]),
            top_routes_30d=[
                RouteHitStats(route=r[0], total_hits=int(r[1]), unique_ips=int(r[2]))
                for r in top_rows
            ],
            hourly_24h=[
                UsageTimeBucket(period=r[0].isoformat() if hasattr(r[0], 'isoformat') else str(r[0]), total_hits=int(r[1]))
                for r in hourly_rows
            ],
            daily_30d=[
                UsageTimeBucket(period=str(r[0]), total_hits=int(r[1]))
                for r in daily_rows
            ],
        )

    usage_stats = UsageStats(
        api=await _source_hits("api"),
        web=await _source_hits("web"),
    )

    return AnalyticsResponse(
        generated_at=now.isoformat(),
        total_services=total_services,
        total_ratings=total_ratings,
        total_categories=total_categories,
        health=HealthOverview(
            by_status=by_status,
            live_percentage=live_pct,
            domain_verified_count=domain_verified_count,
            domain_verified_percentage=domain_verified_pct,
        ),
        pricing=PricingStats(
            avg_sats=round(float(pricing_agg[0] or 0), 1),
            median_sats=compute_median(all_prices),
            min_sats=pricing_agg[1] or 0,
            max_sats=pricing_agg[2] or 0,
            free_count=free_count,
            by_model={r[0]: r[1] for r in by_model_rows},
            by_protocol={r[0]: r[1] for r in by_protocol_rows},
        ),
        categories=[
            CategoryStats(
                name=r[0], slug=r[1], service_count=r[2],
                avg_rating=round(float(r[3]), 1),
                avg_price_sats=round(float(r[4]), 1),
                live_count=int(r[5] or 0),
            ) for r in cat_rows
        ],
        growth=GrowthStats(
            services_added_last_7d=svc_7d,
            services_added_last_30d=svc_30d,
            ratings_added_last_7d=rat_7d,
            ratings_added_last_30d=rat_30d,
            newest_service={
                "name": newest_row[0], "slug": newest_row[1],
                "created_at": newest_row[2].isoformat() if newest_row[2] else None,
            } if newest_row else None,
        ),
        top_rated=[_lb(s) for s in top_rated_rows],
        most_reviewed=[_lb(s) for s in most_reviewed_rows],
        recently_added=[_lb(s) for s in recently_added_rows],
        usage=usage_stats,
    )


async def build_reputation_data(db: AsyncSession, slug: str) -> ReputationResponse:
    service = await get_service_or_404(db, slug)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # --- Service detail ---
    created = service.created_at.replace(tzinfo=None) if service.created_at else now
    age_days = (now - created).days
    service_detail = ServiceDetail(
        name=service.name, slug=service.slug, url=service.url,
        description=service.description, pricing_sats=service.pricing_sats,
        pricing_model=service.pricing_model, protocol=service.protocol,
        owner_name=service.owner_name, logo_url=service.logo_url,
        x402_network=service.x402_network, x402_asset=service.x402_asset,
        x402_pay_to=service.x402_pay_to, pricing_usd=service.pricing_usd,
        domain_verified=service.domain_verified, status=service.status,
        last_probed_at=service.last_probed_at, dead_since=service.dead_since,
        categories=[CategoryOut.model_validate(c) for c in service.categories],
        created_at=service.created_at, age_days=age_days,
    )

    # --- Rating distribution ---
    dist_result = await db.execute(
        select(Rating.score, func.count(Rating.id))
        .where(Rating.service_id == service.id)
        .group_by(Rating.score)
    )
    distribution = {i: 0 for i in range(1, 6)}
    for row in dist_result.all():
        distribution[row[0]] = row[1]
    total_ratings = sum(distribution.values())

    dist_pct = {
        str(k): round(v / total_ratings * 100, 1) if total_ratings else 0.0
        for k, v in distribution.items()
    }
    dist_str = {str(k): v for k, v in distribution.items()}

    rating_summary = RatingSummary(
        avg_rating=service.avg_rating,
        rating_count=service.rating_count,
        distribution=dist_str,
        distribution_pct=dist_pct,
        std_deviation=std_deviation_from_dist(distribution),
        sentiment_label=sentiment_label(service.avg_rating, service.rating_count),
    )

    # --- Monthly trend (SQLite strftime) ---
    trend_rows = (await db.execute(
        select(
            func.strftime('%Y-%m', Rating.created_at),
            func.count(Rating.id),
            func.avg(Rating.score),
        )
        .where(Rating.service_id == service.id)
        .group_by(func.strftime('%Y-%m', Rating.created_at))
        .order_by(func.strftime('%Y-%m', Rating.created_at))
    )).all()
    rating_trend = [
        MonthlyTrend(month=r[0], count=r[1], avg_score=round(float(r[2]), 1))
        for r in trend_rows
    ]

    # --- Peer comparison (based on first category) ---
    peer_comparison = None
    if service.categories:
        primary_cat = service.categories[0]
        peers = (await db.execute(
            select(Service)
            .join(service_categories)
            .where(service_categories.c.category_id == primary_cat.id)
            .where(Service.status != "purged")
        )).scalars().all()

        cat_total = len(peers)
        cat_avg_rating = round(sum(p.avg_rating for p in peers) / cat_total, 1) if cat_total else 0.0
        cat_avg_price = round(sum(p.pricing_sats for p in peers) / cat_total, 1) if cat_total else 0.0

        by_rating = sorted(peers, key=lambda p: (-p.avg_rating, -p.rating_count))
        rating_rank = next((i + 1 for i, p in enumerate(by_rating) if p.id == service.id), 0)
        rating_pctl = round((cat_total - rating_rank) / cat_total * 100, 1) if cat_total and rating_rank else 0.0

        by_price = sorted(peers, key=lambda p: p.pricing_sats)
        price_rank = next((i + 1 for i, p in enumerate(by_price) if p.id == service.id), 0)

        by_volume = sorted(peers, key=lambda p: -p.rating_count)
        volume_rank = next((i + 1 for i, p in enumerate(by_volume) if p.id == service.id), 0)

        higher = [
            PeerEntry(name=p.name, slug=p.slug, avg_rating=p.avg_rating, rating_count=p.rating_count)
            for p in by_rating if p.avg_rating > service.avg_rating and p.id != service.id
        ][:5]
        lower = [
            PeerEntry(name=p.name, slug=p.slug, avg_rating=p.avg_rating, rating_count=p.rating_count)
            for p in reversed(by_rating) if p.avg_rating < service.avg_rating and p.id != service.id
        ][:5]

        peer_comparison = PeerComparison(
            category_avg_rating=cat_avg_rating,
            category_avg_price_sats=cat_avg_price,
            category_total_services=cat_total,
            rating_rank=rating_rank,
            rating_percentile=rating_pctl,
            price_rank=price_rank,
            review_volume_rank=volume_rank,
            peers_rated_higher=higher,
            peers_rated_lower=lower,
        )

    # --- Review activity ---
    activity = (await db.execute(
        select(
            func.min(Rating.created_at),
            func.max(Rating.created_at),
            func.count(func.distinct(Rating.reviewer_name)),
        ).where(Rating.service_id == service.id)
    )).one()
    first_review = activity[0]
    latest_review = activity[1]
    unique_reviewers = activity[2] or 0

    anon_count = (await db.execute(
        select(func.count(Rating.id))
        .where(Rating.service_id == service.id)
        .where(Rating.reviewer_name == "Anonymous")
    )).scalar() or 0

    comment_agg = (await db.execute(
        select(
            func.avg(func.length(Rating.comment)),
            func.sum(case((Rating.comment != "", 1), else_=0)),
        ).where(Rating.service_id == service.id)
    )).one()
    avg_comment_len = round(float(comment_agg[0] or 0), 1)
    with_comments = int(comment_agg[1] or 0)

    review_activity = ReviewActivity(
        first_review_at=first_review,
        latest_review_at=latest_review,
        days_since_last_review=(now - latest_review).days if latest_review else None,
        unique_reviewers=unique_reviewers,
        anonymous_count=anon_count,
        avg_comment_length=avg_comment_len,
        reviews_with_comments=with_comments,
        reviews_without_comments=total_ratings - with_comments,
    )

    # --- Recent reviews ---
    recent = await db.execute(
        select(Rating).where(Rating.service_id == service.id)
        .order_by(Rating.created_at.desc()).limit(20)
    )

    return ReputationResponse(
        generated_at=now.isoformat(),
        service=service_detail,
        rating_summary=rating_summary,
        rating_trend=rating_trend,
        peer_comparison=peer_comparison,
        review_activity=review_activity,
        recent_reviews=[RatingOut.model_validate(r) for r in recent.scalars().all()],
    )


# --- Free Endpoints ---

# IMPORTANT: /services/bulk BEFORE /services/{slug}
@router.get("/services/bulk", response_model=list[ServiceOut])
async def bulk_export(request: Request, db: AsyncSession = Depends(get_db)):
    settlement = await require_payment(
        request=request,
        amount_sats=settings.AUTH_BULK_PRICE_SATS,
        price_usd=settings.AUTH_BULK_PRICE_USD,
        memo="satring.com bulk export",
        resource_url=f"{settings.BASE_URL}/api/v1/services/bulk",
        db=db,
    )
    result = await db.execute(
        select(Service).options(selectinload(Service.categories))
        .where(Service.status != "purged")
        .order_by(Service.id)
    )
    data = [ServiceOut.model_validate(s) for s in result.scalars().all()]
    if settlement:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            content=[d.model_dump(mode="json") for d in data],
            headers={"PAYMENT-RESPONSE": json.dumps(settlement)},
        )
    return data


@router.get("/services", response_model=ServiceListOut)
@limiter.limit(RATE_LIST_API)
async def list_services(
    request: Request,
    category: str | None = None,
    status: str | None = None,
    protocol: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).where(Service.status != "purged").order_by(Service.created_at.desc())
    if category:
        query = query.join(service_categories).join(Category).where(Category.slug == category)
    if status and status in ("unverified", "confirmed", "live", "dead"):
        query = query.where(Service.status == status)
    if protocol and protocol in ("L402", "X402"):
        query = query.where(Service.protocol == protocol)
    return await paginated_services(db, query, page, page_size)


@router.get("/services/{slug}", response_model=ServiceOut)
@limiter.limit(RATE_DETAIL_API)
async def get_service(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    return ServiceOut.model_validate(await get_service_or_404(db, slug))


@router.post("/services", response_model=ServiceCreateOut, status_code=201)
@limiter.limit(RATE_SUBMIT)
async def create_service(request: Request, body: ServiceCreate, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    url_str = normalize_url(str(body.url))

    # Reject duplicate URLs BEFORE payment gate so clients don't pay for a rejected submission
    existing = await find_existing_service(db, url_str)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"A service with this URL already exists: /services/{existing.slug}",
        )

    await require_payment(
        request=request,
        amount_sats=settings.AUTH_SUBMIT_PRICE_SATS,
        price_usd=settings.AUTH_SUBMIT_PRICE_USD,
        memo="satring.com service submission",
        resource_url=f"{settings.BASE_URL}/api/v1/services",
        db=db,
    )

    from app.utils import unique_slug
    slug = await unique_slug(db, body.name)

    # Fetch same-domain services (used for token reuse + auto-verify)
    domain_services = await get_same_domain_services(db, url_str)

    # Check if existing token matches a same-domain service
    token_reused = False
    if body.existing_edit_token:
        for ds in domain_services:
            if ds.edit_token_hash and verify_edit_token(body.existing_edit_token, ds.edit_token_hash):
                token_reused = True
                break

    if token_reused:
        edit_token = body.existing_edit_token
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
    purged = await find_purged_service(db, url_str)
    if purged:
        await overwrite_purged_service(
            db, purged,
            name=body.name, slug=slug, description=body.description,
            pricing_sats=body.pricing_sats, pricing_model=body.pricing_model,
            protocol=body.protocol, owner_name=body.owner_name,
            owner_contact=body.owner_contact, logo_url=body.logo_url,
            edit_token_hash=edit_token_hash,
            category_ids=body.category_ids,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
            x402_network=body.x402_network, x402_asset=body.x402_asset,
            x402_pay_to=body.x402_pay_to, pricing_usd=body.pricing_usd,
        )
        service = purged
    else:
        service = Service(
            name=body.name, slug=slug, url=url_str, description=body.description,
            pricing_sats=body.pricing_sats, pricing_model=body.pricing_model,
            protocol=body.protocol, owner_name=body.owner_name,
            owner_contact=body.owner_contact, logo_url=body.logo_url,
            x402_network=body.x402_network, x402_asset=body.x402_asset,
            x402_pay_to=body.x402_pay_to, pricing_usd=body.pricing_usd,
            edit_token_hash=edit_token_hash,
            domain_verified=auto_verified,
            domain_challenge=inherited_challenge,
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
    out = ServiceCreateOut.model_validate(result.scalars().first())
    out.edit_token = edit_token
    out.token_reused = token_reused

    # Send verification instructions if owner_contact contains an email
    email = extract_email(body.owner_contact or "")
    if email and not auto_verified:
        domain = extract_domain(url_str)
        background_tasks.add_task(send_verify_email, email, service.slug, domain)

    return out


@router.patch("/services/{slug}", response_model=ServiceOut)
@limiter.limit(RATE_EDIT)
async def update_service(
    request: Request,
    slug: str,
    body: ServiceUpdate,
    x_edit_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    service = await get_service_or_404(db, slug)
    if not service.edit_token_hash or not verify_edit_token(x_edit_token, service.edit_token_hash):
        raise HTTPException(status_code=403, detail="Invalid edit token")

    for field in ("name", "description", "pricing_sats", "pricing_model", "protocol", "owner_name", "owner_contact", "logo_url", "x402_network", "x402_asset", "x402_pay_to", "pricing_usd"):
        value = getattr(body, field)
        if value is not None:
            setattr(service, field, value)

    if body.category_ids is not None:
        cats = (await db.execute(
            select(Category).where(Category.id.in_(body.category_ids))
        )).scalars().all()
        service.categories = list(cats)

    await db.commit()
    result = await db.execute(
        select(Service).options(selectinload(Service.categories)).where(Service.id == service.id)
    )
    return ServiceOut.model_validate(result.scalars().first())


@router.delete("/services/{slug}")
@limiter.limit(RATE_DELETE)
async def delete_service(
    request: Request,
    slug: str,
    x_edit_token: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    service = await get_service_or_404(db, slug)
    if not service.edit_token_hash or not verify_edit_token(x_edit_token, service.edit_token_hash):
        raise HTTPException(status_code=403, detail="Invalid edit token")

    await db.delete(service)
    await db.commit()
    return {"deleted": slug}


@router.post("/services/{slug}/recover/generate")
@limiter.limit(RATE_RECOVER)
async def api_recover_generate(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    service = await get_service_or_404(db, slug)
    challenge = secrets.token_hex(32)
    service.domain_challenge = challenge
    service.domain_challenge_expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
    await db.commit()
    return {
        "challenge": challenge,
        "verify_url": f"{domain_root(service.url)}/.well-known/satring-verify",
        "expires_in_minutes": 30,
    }


@router.post("/services/{slug}/recover/verify")
@limiter.limit(RATE_RECOVER)
async def api_recover_verify(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    service = await get_service_or_404(db, slug)
    if (
        not service.domain_challenge
        or not service.domain_challenge_expires_at
        or service.domain_challenge_expires_at <= datetime.now(timezone.utc).replace(tzinfo=None)
    ):
        raise HTTPException(status_code=400, detail="No active challenge or challenge expired")

    verify_url = f"{domain_root(service.url)}/.well-known/satring-verify"

    # SECURITY: Block SSRF — prevent server from fetching internal/private IPs
    hostname = extract_domain(service.url)
    if not hostname or not is_public_hostname(hostname):
        raise HTTPException(status_code=400, detail="Cannot verify domain: hostname resolves to a private or unreachable address")

    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(verify_url)
        fetched = resp.text.strip()
    except Exception:
        raise HTTPException(status_code=502, detail=f"Could not reach {verify_url}")

    if fetched != service.domain_challenge:
        raise HTTPException(status_code=403, detail="Challenge code does not match")

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
    return {
        "edit_token": new_token,
        "affected_services": [ds.slug for ds in domain_services],
    }


@router.get("/search", response_model=ServiceListOut)
@limiter.limit(RATE_SEARCH_API)
async def search_services(
    request: Request,
    q: str = "",
    status: str | None = None,
    protocol: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = select(Service).where(Service.status != "purged").order_by(Service.created_at.desc())
    if q.strip():
        # SECURITY: escape LIKE wildcards so user input is matched literally
        pattern = f"%{escape_like(q.strip())}%"
        query = query.where(Service.name.ilike(pattern, escape="\\") | Service.description.ilike(pattern, escape="\\"))
    if status and status in ("unverified", "confirmed", "live", "dead"):
        query = query.where(Service.status == status)
    if protocol and protocol in ("L402", "X402"):
        query = query.where(Service.protocol == protocol)
    return await paginated_services(db, query, page, page_size)


@router.get("/services/{slug}/ratings", response_model=list[RatingOut])
@limiter.limit(RATE_DETAIL_API)
async def list_ratings(
    request: Request,
    slug: str,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    service = await get_service_or_404(db, slug)
    result = await db.execute(
        select(Rating).where(Rating.service_id == service.id)
        .order_by(Rating.created_at.desc())
        .offset(offset).limit(limit)
    )
    return [RatingOut.model_validate(r) for r in result.scalars().all()]


@router.post("/services/{slug}/ratings", response_model=RatingOut, status_code=201)
@limiter.limit(RATE_REVIEW)
async def create_rating(request: Request, slug: str, body: RatingCreate, db: AsyncSession = Depends(get_db)):
    await require_payment(
        request=request,
        amount_sats=settings.AUTH_REVIEW_PRICE_SATS,
        price_usd=settings.AUTH_REVIEW_PRICE_USD,
        memo="satring.com review submission",
        resource_url=f"{settings.BASE_URL}/api/v1/services/{slug}/ratings",
        db=db,
    )
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
    await require_payment(
        request=request,
        amount_sats=settings.AUTH_ANALYTICS_PRICE_SATS,
        price_usd=settings.AUTH_ANALYTICS_PRICE_USD,
        memo="satring.com analytics access",
        resource_url=f"{settings.BASE_URL}/api/v1/analytics",
        db=db,
    )
    return await build_analytics_data(db)


@router.get("/services/{slug}/reputation")
async def reputation(request: Request, slug: str, db: AsyncSession = Depends(get_db)):
    await require_payment(
        request=request,
        amount_sats=settings.AUTH_REPUTATION_PRICE_SATS,
        price_usd=settings.AUTH_REPUTATION_PRICE_USD,
        memo="satring.com reputation lookup",
        resource_url=f"{settings.BASE_URL}/api/v1/services/{slug}/reputation",
        db=db,
    )
    return await build_reputation_data(db, slug)
