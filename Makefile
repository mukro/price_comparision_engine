# Makefile — Common commands for Price Comparison Engine
.PHONY: help build up down test migrate lint format deploy

# Default target
help:
	@echo "Available commands:"
	@echo "  make build       — Build Docker images"
	@echo "  make up          — Start all services (docker compose up)"
	@echo "  make down        — Stop all services"
	@echo "  make test        — Run test suite"
	@echo "  make migrate     — Run database migrations"
	@echo "  make lint        — Run ruff linter"
	@echo "  make format      — Run ruff formatter"
	@echo "  make deploy      — Deploy to AWS (requires Terraform)"
	@echo "  make logs-api    — Tail API logs"
	@echo "  make logs-worker — Tail Celery worker logs"
	@echo "  make shell-api   — Open shell in API container"
	@echo "  make shell-db    — Open psql in database container"

# Development
build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down -v

logs-api:
	docker compose logs -f api

logs-worker:
	docker compose logs -f celery_worker

shell-api:
	docker compose exec api bash

shell-db:
	docker compose exec db psql -U $$POSTGRES_USER -d $$POSTGRES_DB

# Testing
test:
	pytest tests/ -v --tb=short

test-compliance:
	pytest tests/test_compliance.py -v

# Database
migrate:
	python scripts/run_migrations.py --direction up

migrate-down:
	python scripts/run_migrations.py --direction down

migrate-status:
	python scripts/run_migrations.py --direction status

# Code quality
lint:
	ruff check app/ tests/

format:
	ruff format app/ tests/

# Security
scan-secrets:
	git-secrets --scan

# AWS Deployment
deploy-init:
	cd infra/aws && terraform init

deploy-plan:
	cd infra/aws && terraform plan -out=tfplan

deploy-apply:
	cd infra/aws && terraform apply tfplan

deploy-destroy:
	cd infra/aws && terraform destroy

# Push image to ECR (requires AWS CLI login)
ecr-push:
	$(eval REPO := $(shell cd infra/aws && terraform output -raw ecr_repository_url))
	docker build -t price-comparison:latest .
	docker tag price-comparison:latest $(REPO):latest
	docker push $(REPO):latest

# Force ECS redeployment
ecs-redeploy:
	aws ecs update-service --cluster price-comparison --service api --force-new-deployment
	aws ecs update-service --cluster price-comparison --service worker --force-new-deployment
	aws ecs update-service --cluster price-comparison --service beat --force-new-deployment
