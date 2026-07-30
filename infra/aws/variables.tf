# infra/aws/variables.tf
variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-south-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "prod"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  default     = "price-comparison"
}

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "domain_name" {
  description = "Domain name for ACM certificate (leave empty for HTTP only)"
  type        = string
  default     = ""
}

variable "allowed_origins" {
  description = "Comma-separated CORS origins"
  type        = string
  default     = "https://app.yourdomain.com"
}

variable "sendgrid_api_key" {
  description = "SendGrid API key"
  type        = string
  sensitive   = true
  default     = ""
}

variable "db_name" {
  description = "PostgreSQL database name"
  type        = string
  default     = "price_comparison"
}

variable "db_username" {
  description = "PostgreSQL master username"
  type        = string
  default     = "price_engine"
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GB"
  type        = number
  default     = 20
}

variable "db_max_allocated_storage" {
  description = "RDS max allocated storage in GB"
  type        = number
  default     = 100
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t3.micro"
}

variable "image_tag" {
  description = "Docker image tag to deploy"
  type        = string
  default     = "latest"
}

variable "api_cpu" {
  description = "Fargate CPU units for API (256 = 0.25 vCPU)"
  type        = string
  default     = "512"
}

variable "api_memory" {
  description = "Fargate memory for API (MB)"
  type        = string
  default     = "1024"
}

variable "api_desired_count" {
  description = "Desired number of API tasks"
  type        = number
  default     = 2
}

variable "api_max_count" {
  description = "Maximum number of API tasks (auto-scaling)"
  type        = number
  default     = 6
}

variable "worker_cpu" {
  description = "Fargate CPU units for Celery worker"
  type        = string
  default     = "1024"
}

variable "worker_memory" {
  description = "Fargate memory for Celery worker (MB)"
  type        = string
  default     = "2048"
}

variable "worker_desired_count" {
  description = "Desired number of Celery worker tasks"
  type        = number
  default     = 2
}

variable "scraping_enabled" {
  description = "Global scraping kill-switch"
  type        = bool
  default     = true
}

variable "enforce_robots_txt" {
  description = "Enforce robots.txt checking"
  type        = bool
  default     = true
}

variable "enforce_domain_allowlist" {
  description = "Enforce domain allowlist"
  type        = bool
  default     = true
}

variable "default_scrape_rpm" {
  description = "Default scrape rate limit (requests per minute)"
  type        = number
  default     = 6
}
