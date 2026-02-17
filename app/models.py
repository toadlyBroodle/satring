from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Integer, String, Text, Float, DateTime, ForeignKey, Table
)
from sqlalchemy.orm import relationship

from app.database import Base


service_categories = Table(
    "service_categories",
    Base.metadata,
    Column("service_id", Integer, ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
    Column("category_id", Integer, ForeignKey("categories.id", ondelete="CASCADE"), primary_key=True),
)


class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    slug = Column(String(100), nullable=False, unique=True, index=True)
    description = Column(Text, default="")

    services = relationship("Service", secondary=service_categories, back_populates="categories")


class Service(Base):
    __tablename__ = "services"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    slug = Column(String(200), nullable=False, unique=True, index=True)
    url = Column(String(500), nullable=False)
    description = Column(Text, default="")
    pricing_sats = Column(Integer, default=0)
    pricing_model = Column(String(50), default="per-request")
    protocol = Column(String(10), default="L402")
    owner_name = Column(String(200), default="")
    owner_contact = Column(String(300), default="")
    logo_url = Column(String(500), default="")
    edit_token_hash = Column(String(64), nullable=True, index=True)
    domain_challenge = Column(String(64), nullable=True)
    domain_challenge_expires_at = Column(DateTime, nullable=True)
    domain_verified = Column(Boolean, default=False)
    avg_rating = Column(Float, default=0.0)
    rating_count = Column(Integer, default=0)
    status = Column(String(20), default="unverified")  # confirmed | live | dead
    last_probed_at = Column(DateTime, nullable=True)
    dead_since = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    categories = relationship("Category", secondary=service_categories, back_populates="services")
    ratings = relationship("Rating", back_populates="service", cascade="all, delete-orphan")


class Rating(Base):
    __tablename__ = "ratings"

    id = Column(Integer, primary_key=True)
    service_id = Column(Integer, ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)
    score = Column(Integer, nullable=False)
    comment = Column(Text, default="")
    reviewer_name = Column(String(200), default="Anonymous")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    service = relationship("Service", back_populates="ratings")


