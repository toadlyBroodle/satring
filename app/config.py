import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./db/sr.db")
    PAYMENT_URL: str = os.getenv("PAYMENT_URL", "")
    PAYMENT_KEY: str = os.getenv("PAYMENT_KEY", "")
    AUTH_ROOT_KEY: str = os.getenv("AUTH_ROOT_KEY", "test-mode")
    AUTH_PRICE_SATS: int = int(os.getenv("AUTH_PRICE_SATS", "100"))
    AUTH_SUBMIT_PRICE_SATS: int = int(os.getenv("AUTH_SUBMIT_PRICE_SATS", "1000"))
    AUTH_REVIEW_PRICE_SATS: int = int(os.getenv("AUTH_REVIEW_PRICE_SATS", "10"))
    AUTH_BULK_PRICE_SATS: int = int(os.getenv("AUTH_BULK_PRICE_SATS", "1000"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
    BASE_URL: str = os.getenv("BASE_URL", "https://satring.com")


settings = Settings()

# SECURITY: Input length limits shared across API models, web form handlers,
# and HTML templates. Change values here — not in individual files.
MAX_NAME = 200
MAX_URL = 500
MAX_DESCRIPTION = 5000
MAX_OWNER_NAME = 200
MAX_OWNER_CONTACT = 300
MAX_LOGO_URL = 500
MAX_REVIEWER_NAME = 200
MAX_COMMENT = 2000
MAX_PRICING_SATS = 1_000_000
