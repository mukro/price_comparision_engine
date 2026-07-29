from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "Price Comparison Platform"

    # --- Database ---
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "changeme"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "price_comparison"

    # --- Redis ---
    REDIS_URL: str = "redis://localhost:6379/0"

    # --- Email ---
    SENDGRID_API_KEY: str = ""
    FROM_EMAIL: str = "alerts@pricecomparison.com"

    # --- Auth (JWT) ---
    # NOTE: these three defaults are for local dev only. Always override via
    # environment / .env in any shared or deployed environment.
    JWT_SECRET_KEY: str = "dev-only-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 60

    # Bootstrap admin credentials used by POST /api/v1/admin/auth/login.
    # This is a minimal single-admin login intended to unblock local dev /
    # demos. Swap for a real `users` table + password hashing before
    # handling more than one admin or going to production.
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD: str = "changeme"

    # --- CORS ---
    # Comma-separated list of allowed origins, e.g. "https://app.example.com,https://admin.example.com"
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    @property
    def DATABASE_URL(self) -> str:
        """Sync DSN, used by psycopg2 (Celery workers / core business logic)."""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def ASYNC_DATABASE_URL(self) -> str:
        """Async DSN, used by asyncpg (FastAPI request handlers)."""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.ALLOWED_ORIGINS.split(",") if origin.strip()]

    class Config:
        env_file = ".env"


settings = Settings()
