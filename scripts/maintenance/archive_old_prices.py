#!/usr/bin/env python3
# scripts/maintenance/archive_old_prices.py
"""
Data retention & archival script for price_history table.

Strategy:
  1. Export records older than RETENTION_DAYS to Parquet files
  2. Upload Parquet files to S3 (cheap cold storage)
  3. Delete archived records from PostgreSQL
  4. Vacuum the table to reclaim space

Usage:
    # Dry run (shows what would be archived without doing it)
    python scripts/maintenance/archive_old_prices.py --dry-run

    # Actually archive and delete
    python scripts/maintenance/archive_old_prices.py

    # Archive records older than 180 days
    python scripts/maintenance/archive_old_prices.py --retention-days 180

    # Schedule via cron (weekly on Sunday at 3 AM):
    # 0 3 * * 0 cd /app && python scripts/maintenance/archive_old_prices.py >> /var/log/price_archive.log 2>&1
"""
import argparse
import gzip
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import boto3
import pandas as pd
import psycopg2
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("price_archive")

# Configuration (override via env vars)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "90"))
BATCH_SIZE = int(os.environ.get("ARCHIVE_BATCH_SIZE", "10000"))
S3_BUCKET = os.environ.get("S3_ARCHIVE_BUCKET", "")
S3_PREFIX = os.environ.get("S3_ARCHIVE_PREFIX", "price-history-archives/")
DB_DSN = os.environ.get(
    "DATABASE_URL",
    f"postgresql://{os.environ.get('POSTGRES_USER', 'postgres')}:"
    f"{os.environ.get('POSTGRES_PASSWORD', 'changeme')}@"
    f"{os.environ.get('POSTGRES_HOST', 'localhost')}:"
    f"{os.environ.get('POSTGRES_PORT', '5432')}/"
    f"{os.environ.get('POSTGRES_DB', 'price_comparison')}"
)


def get_cutoff_date(retention_days: int) -> datetime:
    """Returns the cutoff date for archival."""
    return datetime.now(timezone.utc) - timedelta(days=retention_days)


def count_records_to_archive(cursor, cutoff: datetime) -> int:
    """Count how many records are candidates for archival."""
    cursor.execute(
        """
        SELECT COUNT(*) FROM price_history
        WHERE recorded_at < %s;
        """,
        (cutoff,),
    )
    return cursor.fetchone()[0]


def fetch_batch(cursor, cutoff: datetime, limit: int):
    """Yield batches of old records."""
    cursor.execute(
        """
        SELECT
            ph.id, ph.offer_id, ph.price, ph.in_stock, ph.recorded_at,
            vo.vendor_id, vo.product_id, v.domain AS vendor_domain
        FROM price_history ph
        JOIN vendor_offers vo ON ph.offer_id = vo.id
        JOIN vendors v ON vo.vendor_id = v.id
        WHERE ph.recorded_at < %s
        ORDER BY ph.recorded_at ASC
        LIMIT %s;
        """,
        (cutoff, limit),
    )
    columns = [desc[0] for desc in cursor.description]
    while True:
        rows = cursor.fetchmany(BATCH_SIZE)
        if not rows:
            break
        yield pd.DataFrame(rows, columns=columns)


def upload_to_s3(local_path: Path, s3_key: str) -> bool:
    """Upload a file to S3. Returns True on success."""
    if not S3_BUCKET:
        logger.warning("S3_ARCHIVE_BUCKET not set; skipping S3 upload.")
        return False

    try:
        s3 = boto3.client("s3")
        s3.upload_file(
            str(local_path),
            S3_BUCKET,
            s3_key,
            ExtraArgs={
                "ServerSideEncryption": "AES256",
                "StorageClass": "GLACIER_IR",  # Instant Retrieval — cheap, fast access
            },
        )
        logger.info(f"Uploaded to s3://{S3_BUCKET}/{s3_key}")
        return True
    except ClientError as e:
        logger.error(f"S3 upload failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected S3 error: {e}")
        return False


def archive_batch(
    df: pd.DataFrame,
    archive_date: str,
    batch_num: int,
    dry_run: bool,
) -> Optional[str]:
    """
    Archive a DataFrame to Parquet and upload to S3.
    Returns the S3 key on success, None on failure.
    """
    if df.empty:
        return None

    filename = f"price_history_{archive_date}_batch{batch_num:04d}.parquet"
    s3_key = f"{S3_PREFIX}{archive_date}/{filename}"

    if dry_run:
        logger.info(f"[DRY RUN] Would archive {len(df)} records to {s3_key}")
        return s3_key

    with tempfile.TemporaryDirectory() as tmpdir:
        local_path = Path(tmpdir) / filename

        # Write to Parquet with compression
        df.to_parquet(
            local_path,
            engine="pyarrow",
            compression="zstd",
            index=False,
        )

        file_size = local_path.stat().st_size
        logger.info(f"Written {len(df)} records to {filename} ({file_size / 1024:.1f} KB)")

        if upload_to_s3(local_path, s3_key):
            return s3_key

    return None


def delete_archived_records(cursor, cutoff: datetime, dry_run: bool) -> int:
    """Delete records older than cutoff. Returns number of rows deleted."""
    if dry_run:
        cursor.execute(
            "SELECT COUNT(*) FROM price_history WHERE recorded_at < %s;",
            (cutoff,),
        )
        count = cursor.fetchone()[0]
        logger.info(f"[DRY RUN] Would delete {count} records from price_history")
        return count

    cursor.execute(
        "DELETE FROM price_history WHERE recorded_at < %s;",
        (cutoff,),
    )
    return cursor.rowcount


def vacuum_table(conn):
    """Run VACUUM to reclaim space. Must be outside a transaction."""
    conn.autocommit = True
    with conn.cursor() as cursor:
        logger.info("Running VACUUM ANALYZE on price_history...")
        cursor.execute("VACUUM ANALYZE price_history;")
    conn.autocommit = False


def write_manifest(s3_keys: list[str], archive_date: str):
    """Write a manifest file listing all archived batches."""
    if not S3_BUCKET or not s3_keys:
        return

    manifest = {
        "archive_date": archive_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retention_days": RETENTION_DAYS,
        "total_batches": len(s3_keys),
        "batches": s3_keys,
    }

    manifest_key = f"{S3_PREFIX}{archive_date}/manifest.json"
    try:
        s3 = boto3.client("s3")
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=manifest_key,
            Body=json.dumps(manifest, indent=2),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        logger.info(f"Manifest written to s3://{S3_BUCKET}/{manifest_key}")
    except Exception as e:
        logger.error(f"Failed to write manifest: {e}")


def main():
    parser = argparse.ArgumentParser(description="Archive old price_history records")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=RETENTION_DAYS,
        help=f"Archive records older than N days (default: {RETENTION_DAYS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be archived without making changes",
    )
    parser.add_argument(
        "--no-vacuum",
        action="store_true",
        help="Skip VACUUM after deletion",
    )
    args = parser.parse_args()

    cutoff = get_cutoff_date(args.retention_days)
    archive_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    logger.info("=" * 60)
    logger.info("Price History Archival Job")
    logger.info(f"Cutoff date: {cutoff.isoformat()}")
    logger.info(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    logger.info("=" * 60)

    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = False

    try:
        with conn.cursor() as cursor:
            # Step 1: Count
            total_to_archive = count_records_to_archive(cursor, cutoff)
            logger.info(f"Records to archive: {total_to_archive}")

            if total_to_archive == 0:
                logger.info("Nothing to archive. Exiting.")
                return

            # Step 2: Archive in batches
            s3_keys = []
            batch_num = 0
            total_archived = 0

            for df in fetch_batch(cursor, cutoff, limit=BATCH_SIZE):
                batch_num += 1
                s3_key = archive_batch(df, archive_date, batch_num, args.dry_run)
                if s3_key:
                    s3_keys.append(s3_key)
                    total_archived += len(df)
                else:
                    logger.error(f"Batch {batch_num} failed to archive. Aborting.")
                    conn.rollback()
                    sys.exit(1)

            logger.info(f"Successfully archived {total_archived} records in {batch_num} batches")

            # Step 3: Write manifest
            if not args.dry_run:
                write_manifest(s3_keys, archive_date)

            # Step 4: Delete from DB
            deleted = delete_archived_records(cursor, cutoff, args.dry_run)
            logger.info(f"Deleted {deleted} records from price_history")

            if not args.dry_run:
                conn.commit()
                logger.info("Changes committed.")

                # Step 5: Vacuum
                if not args.no_vacuum:
                    vacuum_table(conn)
            else:
                conn.rollback()
                logger.info("[DRY RUN] Changes rolled back.")

    except Exception as e:
        logger.exception("Archival job failed")
        conn.rollback()
        sys.exit(1)
    finally:
        conn.close()

    logger.info("Archival job completed successfully.")


if __name__ == "__main__":
    main()
