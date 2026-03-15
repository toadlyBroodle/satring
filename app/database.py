import sqlalchemy
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Migrate: add x402 columns to existing services table if missing
    async with engine.begin() as conn:
        result = await conn.execute(sqlalchemy.text("PRAGMA table_info(services)"))
        existing_cols = {row[1] for row in result.fetchall()}
        migrations = [
            ("x402_network", "VARCHAR(50)"),
            ("x402_asset", "VARCHAR(100)"),
            ("x402_pay_to", "VARCHAR(100)"),
            ("pricing_usd", "VARCHAR(20)"),
            ("avg_latency_ms", "FLOAT"),
            ("total_checks", "INTEGER DEFAULT 0"),
            ("successful_checks", "INTEGER DEFAULT 0"),
        ]
        for col_name, col_type in migrations:
            if col_name not in existing_cols:
                await conn.execute(
                    sqlalchemy.text(f"ALTER TABLE services ADD COLUMN {col_name} {col_type}")
                )
