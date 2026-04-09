import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.config import settings
from app.database import Base, get_db
from app.models import Category, Service, Rating
from app.main import app, limiter, SEED_CATEGORIES

# Bypass L402 paywall in tests
settings.AUTH_ROOT_KEY = "test-mode"

_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://satring:cYBv_gUZY0QgsnzD2a0Fr2jm_1_aU00jlohHQVGE_wo@localhost/satring_test",
)


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


async def _make_db():
    """Create a fresh DB engine + seeded session."""
    engine = create_async_engine(_TEST_DB_URL, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session = session_factory()
    for name, slug, description in SEED_CATEGORIES:
        session.add(Category(name=name, slug=slug, description=description))
    await session.commit()
    return engine, session


async def _teardown_db(engine, session):
    await session.close()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db():
    engine, session = await _make_db()
    yield session
    await _teardown_db(engine, session)


@pytest_asyncio.fixture(scope="class")
async def class_db():
    """Class-scoped DB: all tests in a class share one database."""
    engine, session = await _make_db()
    yield session
    await _teardown_db(engine, session)


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture(scope="class")
async def class_client(class_db: AsyncSession):
    """Class-scoped client sharing the class_db session."""
    async def override_get_db():
        yield class_db

    app.dependency_overrides[get_db] = override_get_db
    limiter.reset()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_service(db: AsyncSession) -> Service:
    cats = (await db.execute(select(Category).where(Category.slug.in_(["ai-ml", "tools"])))).scalars().all()
    svc = Service(
        name="Test API", slug="test-api", url="https://api.test.com",
        description="A test Lightning API", pricing_sats=100,
        pricing_model="per-request", protocol="L402",
        owner_name="Tester", owner_contact="test@test.com",
    )
    svc.categories = list(cats)
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc


@pytest_asyncio.fixture
async def sample_x402_service(db: AsyncSession) -> Service:
    cats = (await db.execute(select(Category).where(Category.slug.in_(["finance"])))).scalars().all()
    svc = Service(
        name="X402 Pay API", slug="x402-pay-api", url="https://x402.test.com",
        description="An x402 USDC payment API", pricing_sats=0,
        pricing_model="per-request", protocol="x402",
        owner_name="X402 Tester", owner_contact="x402@test.com",
        x402_network="eip155:8453",
        x402_asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        x402_pay_to="0xTestWallet123",
        pricing_usd="0.05",
    )
    svc.categories = list(cats)
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc


@pytest_asyncio.fixture
async def sample_dual_service(db: AsyncSession) -> Service:
    cats = (await db.execute(select(Category).where(Category.slug.in_(["tools"])))).scalars().all()
    svc = Service(
        name="Dual Protocol API", slug="dual-proto-api", url="https://dual.test.com",
        description="A dual L402+x402 API", pricing_sats=50,
        pricing_model="per-request", protocol="L402+x402",
        owner_name="Dual Tester", owner_contact="dual@test.com",
        x402_network="eip155:8453",
        x402_asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        x402_pay_to="0xDualWallet456",
        pricing_usd="0.02",
    )
    svc.categories = list(cats)
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc


@pytest_asyncio.fixture
async def sample_mpp_service(db: AsyncSession) -> Service:
    cats = (await db.execute(select(Category).where(Category.slug.in_(["tools"])))).scalars().all()
    svc = Service(
        name="MPP Pay API", slug="mpp-pay-api", url="https://mpp.test.com",
        description="An MPP payment API", pricing_sats=0,
        pricing_model="per-request", protocol="MPP",
        mpp_method="tempo", mpp_realm="api.test.com",
        mpp_currency="usd",
    )
    svc.categories = list(cats)
    db.add(svc)
    await db.commit()
    await db.refresh(svc)
    return svc


@pytest_asyncio.fixture
async def sample_service_with_ratings(db: AsyncSession, sample_service: Service) -> Service:
    for score, comment, name in [
        (5, "Excellent", "Alice"),
        (4, "Pretty good", "Bob"),
        (3, "Average", "Charlie"),
    ]:
        db.add(Rating(
            service_id=sample_service.id, score=score,
            comment=comment, reviewer_name=name,
        ))
    await db.flush()

    # Update denormalized fields
    sample_service.avg_rating = 4.0
    sample_service.rating_count = 3
    await db.commit()
    await db.refresh(sample_service)
    return sample_service
