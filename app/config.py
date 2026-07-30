# app/config.py
from pydantic import Field, validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Price Comparison Platform"

    # --- Database (NO defaults — fail fast if missing) ---
    POSTGRES_USER: str = Field(..., min_length=1)
    POSTGRES_PASSWORD: str = Field(..., min_length=8)
    POSTGRES_HOST: str = Field(..., min_length=1)
    POSTGRES_PORT: int = Field(default=5432, ge=1, le=65535)
    POSTGRES_DB: str = Field(..., min_length=1)

    # --- Redis ---
    REDIS_URL: str = Field(..., min_length=1)

    # --- Email ---
    SENDGRID_API_KEY: str = Field(default="")
    FROM_EMAIL: str = Field(default="alerts@pricecomparison.com")

    # --- Auth (JWT) ---
    JWT_SECRET_KEY: str = Field(..., min_length=32)
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = Field(default=60, ge=5)

    # Admin credentials — MUST be bcrypt hash in production.
    # Generate once: python -c "from passlib.hash import bcrypt; print(bcrypt.hash('yourpassword'))"
    ADMIN_EMAIL: str = Field(default="admin@example.com")
    ADMIN_PASSWORD_HASH: str = Field(
        default="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"  # hash of "changeme"
    )

    # --- CORS ---
    ALLOWED_ORIGINS: str = Field(default="http://localhost:3000")

    # --- Compliance / Scraping Governance ---
    # Master switch: if False, ALL scraping tasks become no-ops.
    SCRAPING_ENABLED: bool = Field(default=True)
    # Default requests-per-minute cap per domain when no vendor-specific rule exists.
    DEFAULT_SCRAPE_RPM: int = Field(default=6, ge=1, le=120)
    # If True, every scrape checks robots.txt and aborts if disallowed.
    ENFORCE_ROBOTS_TXT: bool = Field(default=True)
    # If True, only domains explicitly allow-listed in the DB can be scraped.
    ENFORCE_DOMAIN_ALLOWLIST: bool = Field(default=False)
    # Global user-agent string.
    SCRAPER_USER_AGENT: str = Field(
        default="Mozilla/5.0 (compatible; PriceComparisonBot/1.0; +https://example.com/bot)"
    )
    # Playwright proxy (optional), e.g. "http://proxy:8080"
    SCRAPER_PROXY_URL: str = Field(default="")
    # Max browser pages to keep open per worker process.
    BROWSER_MAX_PAGES: int = Field(default=4, ge=1, le=20)
    # Scrape timeout in milliseconds.
    SCRAPER_TIMEOUT_MS: int = Field(default=15000, ge=5000, le=60000)

    @property
    def DATABASE_URL(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def ASYNC_DATABASE_URL(self) -> str:
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        case_sensitive = False


settings = Settings()
