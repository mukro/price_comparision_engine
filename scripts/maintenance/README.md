# Database Maintenance Scripts

## archive_old_prices.py

Archives old `price_history` records to S3 (Parquet format) and deletes them from PostgreSQL to keep the database lean.

### Why Archive?

- `price_history` is append-only and grows indefinitely
- Query performance degrades with hundreds of millions of rows
- S3 + Parquet is 10x cheaper than RDS storage for cold data
- Parquet files can be queried with Athena / DuckDB for analytics

### Workflow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  price_history  │────▶│  Parquet files  │────▶│   S3 Glacier  │
│  (PostgreSQL)   │     │  (compressed)   │     │  (cold storage) │
└─────────────────┘     └─────────────────┘     └─────────────────┘
         │
         ▼
   VACUUM ANALYZE
   (reclaim space)
```

### Usage

```bash
# Dry run (safe — shows what would happen)
python scripts/maintenance/archive_old_prices.py --dry-run

# Live archive (records older than 90 days)
python scripts/maintenance/archive_old_prices.py

# Custom retention
python scripts/maintenance/archive_old_prices.py --retention-days 180

# Skip vacuum (faster, but space not reclaimed)
python scripts/maintenance/archive_old_prices.py --no-vacuum
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `RETENTION_DAYS` | 90 | Archive records older than N days |
| `ARCHIVE_BATCH_SIZE` | 10000 | Records per batch |
| `S3_ARCHIVE_BUCKET` | — | S3 bucket for archives |
| `S3_ARCHIVE_PREFIX` | `price-history-archives/` | S3 key prefix |
| `DATABASE_URL` | — | PostgreSQL connection string |

### S3 Storage Class

Files are stored in **S3 Glacier Instant Retrieval**:
- ~$0.004/GB/month (vs $0.115/GB for RDS)
- Millisecond access time for analytics queries
- Ideal for compliance / audit data

### Scheduling

Add to crontab for weekly archival:

```bash
# Edit crontab
crontab -e

# Add this line (Sunday 3 AM)
0 3 * * 0 cd /app && python scripts/maintenance/archive_old_prices.py >> /var/log/price_archive.log 2>&1
```

Or use Celery Beat (add to `app/celery_app.py`):

```python
"archive-old-price-history-weekly": {
    "task": "app.tasks.run_price_history_archive",
    "schedule": crontab(day_of_week=0, hour=3, minute=0),
},
```

### Restoring Archived Data

```python
import pandas as pd

# Read a specific archive
df = pd.read_parquet("s3://your-bucket/price-history-archives/2024-01-15/price_history_2024-01-15_batch0001.parquet")

# Query multiple archives with DuckDB
import duckdb
con = duckdb.connect()
con.execute("""
    SELECT * FROM read_parquet('s3://your-bucket/price-history-archives/*/*/*.parquet')
    WHERE product_id = '550e8400-e29b-41d4-a716-446655440000'
""")
```

### Dependencies

Add to `requirements.txt`:

```
pandas>=2.2.0
pyarrow>=15.0.0
boto3>=1.34.0
```

### IAM Permissions (for EC2/ECS)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject"
      ],
      "Resource": "arn:aws:s3:::your-archive-bucket/*"
    }
  ]
}
```
