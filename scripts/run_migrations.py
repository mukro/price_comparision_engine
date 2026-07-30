#!/usr/bin/env python3
# scripts/run_migrations.py
"""
Programmatic database migration runner using asyncpg.
Safe, idempotent, and logged.

Usage:
    python scripts/run_migrations.py --direction up
    python scripts/run_migrations.py --direction down
"""
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

import asyncpg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("migration")

# Load .env if present
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                key, val = line.strip().split("=", 1)
                os.environ.setdefault(key, val)

DSN = os.environ.get(
    "ASYNC_DATABASE_URL",
    f"postgresql://{os.environ.get('POSTGRES_USER','postgres')}:"
    f"{os.environ.get('POSTGRES_PASSWORD','changeme')}@"
    f"{os.environ.get('POSTGRES_HOST','localhost')}:"
    f"{os.environ.get('POSTGRES_PORT','5432')}/"
    f"{os.environ.get('POSTGRES_DB','price_comparison')}"
)

MIGRATIONS_DIR = Path(__file__).parent


async def ensure_migration_table(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            id SERIAL PRIMARY KEY,
            filename VARCHAR(255) NOT NULL UNIQUE,
            applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            checksum VARCHAR(64) NOT NULL
        );
        """
    )


async def get_applied_migrations(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch("SELECT filename FROM schema_migrations;")
    return {r["filename"] for r in rows}


async def compute_checksum(filepath: Path) -> str:
    import hashlib
    content = filepath.read_bytes()
    return hashlib.sha256(content).hexdigest()


async def apply_migration(conn: asyncpg.Connection, filepath: Path) -> None:
    sql = filepath.read_text()
    checksum = await compute_checksum(filepath)
    filename = filepath.name

    logger.info(f"Applying migration: {filename}")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2);",
            filename, checksum,
        )
    logger.info(f"Migration applied: {filename}")


async def rollback_migration(conn: asyncpg.Connection, filepath: Path) -> None:
    """
    Rollback support: looks for a matching .down.sql file.
    If not found, warns and skips.
    """
    down_file = filepath.with_suffix(".down.sql")
    if not down_file.exists():
        logger.warning(f"No rollback file found for {filepath.name}; skipping.")
        return

    sql = down_file.read_text()
    filename = filepath.name
    logger.info(f"Rolling back migration: {filename}")
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute(
            "DELETE FROM schema_migrations WHERE filename = $1;",
            filename,
        )
    logger.info(f"Rollback complete: {filename}")


async def run_migrations(direction: str = "up") -> None:
    conn = await asyncpg.connect(DSN)
    try:
        await ensure_migration_table(conn)
        applied = await get_applied_migrations(conn)

        # Find all .sql files in migrations dir, sorted
        files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        # Exclude .down.sql files from the up list
        up_files = [f for f in files if not f.name.endswith(".down.sql")]

        if direction == "up":
            for filepath in up_files:
                if filepath.name in applied:
                    logger.info(f"Skipping already applied: {filepath.name}")
                    continue
                await apply_migration(conn, filepath)

        elif direction == "down":
            # Rollback last applied migration
            last_applied = await conn.fetch(
                "SELECT filename FROM schema_migrations ORDER BY applied_at DESC LIMIT 1;"
            )
            if not last_applied:
                logger.info("No migrations to rollback.")
                return
            last_file = MIGRATIONS_DIR / last_applied[0]["filename"]
            await rollback_migration(conn, last_file)

        elif direction == "status":
            logger.info(f"Applied migrations: {len(applied)}")
            for f in sorted(applied):
                logger.info(f"  ✓ {f}")
            pending = [f.name for f in up_files if f.name not in applied]
            if pending:
                logger.info(f"Pending migrations: {len(pending)}")
                for f in pending:
                    logger.info(f"  ○ {f}")
            else:
                logger.info("No pending migrations.")

        else:
            logger.error(f"Unknown direction: {direction}")
            sys.exit(1)

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Database migration runner")
    parser.add_argument(
        "--direction",
        choices=["up", "down", "status"],
        default="up",
        help="Migration direction",
    )
    args = parser.parse_args()
    asyncio.run(run_migrations(args.direction))
