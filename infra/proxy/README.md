# Reverse Proxy Configuration

Choose **Nginx** or **Traefik** based on your infrastructure preference.

## Option A: Nginx (Traditional, Battle-Tested)

**Best for:** EC2 instances, VM-based deployments, teams familiar with Nginx.

```bash
# Run Nginx in Docker
docker run -d \
  --name price_nginx \
  -p 80:80 -p 443:443 \
  -v $(pwd)/infra/proxy/nginx.conf:/etc/nginx/nginx.conf:ro \
  -v /etc/letsencrypt:/etc/nginx/ssl:ro \
  --network app_network \
  nginx:alpine
```

**Features:**
- SSL termination with modern cipher suites
- Rate limiting per endpoint (search: 30/min, grid: 60/min, alerts: 10/min)
- Gzip compression
- Security headers (CSP, HSTS, X-Frame-Options)
- Upstream health checks
- Keepalive connections

## Option B: Traefik (Cloud-Native, Auto-Discovery)

**Best for:** Docker Swarm, Kubernetes, teams wanting automatic SSL.

```bash
# Run Traefik
cd infra/proxy
docker compose -f docker-compose.traefik.yml up -d
```

**Features:**
- Automatic Let's Encrypt SSL certificates
- Docker service discovery (no config changes needed when scaling)
- Built-in dashboard with basic auth
- Circuit breaker middleware
- Retry logic
- Rate limiting per route
- Compress middleware

### Traefik Dashboard

Access at `https://traefik.yourdomain.com` (protected by basic auth).

Default credentials: `admin` / `admin` — **change this immediately** in `dynamic.yml`.

## SSL Certificates

### Let's Encrypt (Recommended)
Both Nginx and Traefik configs support Let's Encrypt. Traefik handles this automatically. For Nginx, use Certbot:

```bash
# Install Certbot
certbot certonly --standalone -d api.yourdomain.com

# Auto-renewal cron
echo "0 2 * * * certbot renew --quiet" | sudo crontab -
```

### AWS ACM (For ALB)
If using the Terraform ALB setup, SSL is handled by ACM. No proxy SSL needed — terminate at ALB.

## Rate Limiting Reference

| Endpoint | Limit | Burst |
|----------|-------|-------|
| `/api/v1/products/search` | 30/min | 10 |
| `/api/v1/products/{id}/grid` | 60/min | 20 |
| `/api/v1/alerts` | 10/min | 5 |
| `/health` | Unlimited | — |

## Troubleshooting

| Issue | Solution |
|-------|----------|
| 502 Bad Gateway | Check API container health (`docker compose ps`) |
| Rate limit errors | Increase limits in config or scale API containers |
| SSL certificate errors | Verify cert path and permissions |
| Traefik dashboard 404 | Ensure `traefik.enable=true` label on services |
