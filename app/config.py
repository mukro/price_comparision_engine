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
    REDIS_PASSWORD: str = Field(default="")   # <-- FIX: docker-compose uses this

    # --- Email ---
    SENDGRID_API_KEY: str = Field(default="")
    FROM_EMAIL: str = Field(default="alerts@pricecomparison.com")

    # --- Auth (JWT) ---
    JWT_SECRET_KEY: str = Field(..., min_length=32)
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = Field(default=60, ge=5)

    # Admin credentials — MUST be bcrypt hash in production.
    ADMIN_EMAIL: str = Field(default="admin@example.com")
    ADMIN_PASSWORD_HASH: str = Field(
        default="$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW" # hash of "changeme"
    )

    # --- CORS ---
    ALLOWED_ORIGINS: str = Field(default="http://localhost:3000")

    # --- Razorpay (AutoBuy payment gateway) ---
    RAZORPAY_KEY_ID: str = Field(default="")
    RAZORPAY_KEY_SECRET: str = Field(default="")

    # --- FCM / Push Notifications ---
    FCM_SERVER_KEY: str = Field(default="")
    FCM_PROJECT_ID: str = Field(default="")

    # --- Agentic AI (tasks_agents.py) ---
    AGENTS_ENABLED: bool = Field(default=False)

    # --- Compliance / Scraping Governance ---
    SCRAPING_ENABLED: bool = Field(default=True)
    DEFAULT_SCRAPE_RPM: int = Field(default=6, ge=1, le=120)
    ENFORCE_ROBOTS_TXT: bool = Field(default=True)
    ENFORCE_DOMAIN_ALLOWLIST: bool = Field(default=False)
    SCRAPER_USER_AGENT: str = Field(
        default="Mozilla/5.0 (compatible; PriceComparisonBot/1.0; +https://example.com/bot)"
    )
    SCRAPER_PROXY_URL: str = Field(default="")
    BROWSER_MAX_PAGES: int = Field(default=4, ge=1, le=20)
    SCRAPER_TIMEOUT_MS: int = Field(default=15000, ge=5000, le=60000)

    # --- Flower / Monitoring ---
    FLOWER_USER: str = Field(default="admin")
    FLOWER_PASSWORD: str = Field(default="admin")

    # --- Grafana ---
    GRAFANA_ADMIN_PASSWORD: str = Field(default="admin")

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
