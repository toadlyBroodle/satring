import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./satring.db")
    PAYMENT_URL: str = os.getenv("PAYMENT_URL", "")
    PAYMENT_KEY: str = os.getenv("PAYMENT_KEY", "")
    AUTH_ROOT_KEY: str = os.getenv("AUTH_ROOT_KEY", "test-mode")
    AUTH_PRICE_SATS: int = int(os.getenv("AUTH_PRICE_SATS", "100"))
    AUTH_SUBMIT_PRICE_SATS: int = int(os.getenv("AUTH_SUBMIT_PRICE_SATS", "1000"))
    AUTH_REVIEW_PRICE_SATS: int = int(os.getenv("AUTH_REVIEW_PRICE_SATS", "100"))
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")


settings = Settings()
