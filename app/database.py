"""
Synchronous SQLAlchemy session factory for Celery tasks.
FastAPI request handlers use asyncpg via app.db instead.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=3600,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI-style dependency generator (sync)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
