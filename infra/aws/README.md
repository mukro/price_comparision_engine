# Infrastructure Setup (AWS)

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                         Internet                              │
└──────────────────────┬──────────────────────────────────────┘
                       │ HTTPS (443)
                       ▼
┌─────────────────────────────────────────────────────────────┐
│              Application Load Balancer (ALB)                 │
│         • ACM SSL Certificate (if domain configured)         │
│         • HTTP → HTTPS redirect                              │
└──────────────────────┬──────────────────────────────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ┌─────────┐   ┌─────────┐   ┌─────────┐
   │ API svc │   │ API svc │   │ API svc │   ECS Fargate (auto-scaling)
   │  :8000  │   │  :8000  │   │  :8000  │   • Health checks on /health
   └─────────┘   └─────────┘   └─────────┘
        │              │              │
        └──────────────┼──────────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ┌─────────┐   ┌─────────┐   ┌─────────┐
   │ Worker  │   │ Worker  │   │  Beat   │   Celery (background tasks)
   │ (scrape)│   │ (scrape)│   │(scheduler)
   └─────────┘   └─────────┘   └─────────┘
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────────┐
   │   RDS    │  │ElastiCache│  │ SecretsMgr   │
   │PostgreSQL│  │  Redis   │  │   (KMS)      │
   │+ pgvector│  │  (TLS)   │  │              │
   └──────────┘  └──────────┘  └──────────────┘
```

## Prerequisites

1. **AWS CLI** configured with appropriate credentials
2. **Terraform** >= 1.5 installed
3. **Docker** installed locally
4. **Domain name** (optional — ALB works with DNS name for dev)

## Quick Start

### 1. Bootstrap Terraform Backend

```bash
cd infra/aws

# Create S3 bucket for state (one-time)
aws s3 mb s3://price-engine-tfstate --region ap-south-1
aws s3api put-bucket-versioning   --bucket price-engine-tfstate   --versioning-configuration Status=Enabled

# Create DynamoDB table for state locking (one-time)
aws dynamodb create-table   --table-name terraform-locks   --attribute-definitions AttributeName=LockID,AttributeType=S   --key-schema AttributeName=LockID,KeyType=HASH   --billing-mode PAY_PER_REQUEST   --region ap-south-1
```

### 2. Initialize Terraform

```bash
terraform init
```

### 3. Create a `terraform.tfvars` file

```hcl
aws_region        = "ap-south-1"
environment       = "prod"
project_name      = "price-comparison"
domain_name       = "api.yourdomain.com"  # Optional
allowed_origins   = "https://app.yourdomain.com,https://admin.yourdomain.com"
sendgrid_api_key  = "SG.xxx..."

# Instance sizing (adjust for load)
db_instance_class    = "db.t3.micro"   # Start small, scale up
db_allocated_storage = 20
redis_node_type      = "cache.t3.micro"

api_desired_count   = 2
api_max_count       = 6
worker_desired_count = 2

# Scraping governance
scraping_enabled         = true
enforce_robots_txt       = true
enforce_domain_allowlist = true
default_scrape_rpm       = 6
```

### 4. Plan & Apply

```bash
terraform plan -out=tfplan
terraform apply tfplan
```

### 5. Build & Push Docker Image

```bash
# Login to ECR
aws ecr get-login-password --region ap-south-1 |   docker login --username AWS --password-stdin $(terraform output -raw ecr_repository_url)

# Build and tag
docker build -t price-comparison:latest .
docker tag price-comparison:latest $(terraform output -raw ecr_repository_url):latest

# Push
docker push $(terraform output -raw ecr_repository_url):latest
```

### 6. Deploy to ECS

```bash
# Update ECS services to use the new image
aws ecs update-service   --cluster price-comparison   --service api   --force-new-deployment

aws ecs update-service   --cluster price-comparison   --service worker   --force-new-deployment

aws ecs update-service   --cluster price-comparison   --service beat   --force-new-deployment
```

## CI/CD (GitHub Actions)

The included `.github/workflows/deploy.yml` automatically:

1. **Tests** your code against a PostgreSQL + Redis service container
2. **Builds** a Docker image and pushes to ECR
3. **Deploys** to ECS with zero-downtime rolling updates

### Required GitHub Secrets

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Description |
|--------|-------------|
| `AWS_ACCESS_KEY_ID` | IAM user access key |
| `AWS_SECRET_ACCESS_KEY` | IAM user secret key |

### Optional: Domain & SSL

If you set `domain_name`, you must create a DNS CNAME record:

```
api.yourdomain.com  CNAME  <alb-dns-name>
```

The ACM certificate validation will happen automatically if you use Route 53. For external DNS providers, manually add the validation CNAME records shown in the AWS Console.

## Security Checklist

- [ ] RDS is in private subnets (no public access)
- [ ] Redis uses `transit_encryption_enabled = true`
- [ ] Secrets are in Secrets Manager, not in code
- [ ] S3 bucket has public access blocked
- [ ] ECS tasks run in private subnets
- [ ] ALB security group only allows 443/80
- [ ] CloudWatch logs encrypted with KMS
- [ ] ECR images scanned on push

## Cost Optimization (Dev/Staging)

For non-production environments, use `single_nat_gateway = true` and smaller instance types:

```hcl
environment       = "dev"
db_instance_class = "db.t3.micro"
redis_node_type   = "cache.t3.micro"
api_desired_count = 1
worker_desired_count = 1
```

Expected monthly cost: **~$50-80 USD** (vs ~$200+ for prod with HA).

## Monitoring

- **CloudWatch Logs**: `/ecs/price-comparison`
- **CloudWatch Metrics**: ECS CPU/Memory, RDS connections, Redis cache hits
- **RDS Performance Insights**: Query-level performance analysis
- **ALB Access Logs**: Stored in S3 bucket

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Tasks stuck in `PENDING` | Check VPC NAT Gateway (private subnets need outbound internet) |
| `CannotPullContainerError` | Verify ECR IAM policy on task execution role |
| DB connection timeout | Check security group rules (ECS → RDS on port 5432) |
| Redis auth failures | Verify `REDIS_URL` includes auth token and uses `rediss://` |
| High API latency | Scale up `api_desired_count` or upgrade `api_cpu`/`api_memory` |
