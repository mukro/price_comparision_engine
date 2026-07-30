# docs/openapi_enhancements.py
"""
OpenAPI / Swagger enhancements for FastAPI.
Import and call enhance_openapi(app) in app/main.py to apply.

Usage in main.py:
    from docs.openapi_enhancements import enhance_openapi
    enhance_openapi(app)
"""
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


def enhance_openapi(app: FastAPI) -> None:
    """
    Overrides the default OpenAPI schema with rich metadata,
    security schemes, and tagged operation grouping.
    """

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema

        openapi_schema = get_openapi(
            title="Price Comparison Engine API",
            version="2.0.0",
            description="""
## 🏷️ Price Comparison Engine

Multi-vendor product search, price grid comparison, historical trend tracking,
and AI-powered dynamic pricing for B2B merchants.

### Key Features
- **Hybrid Search**: pgvector semantic + full-text (RRF)
- **Real-time Scraping**: Playwright-based with compliance governance
- **Dynamic Pricing**: Merchant rules with circuit breakers
- **Price Alerts**: Email notifications on genuine price drops
- **Buy Timing Insights**: 90-day historical analysis

### Authentication
All admin and merchant endpoints require a Bearer token:
```
Authorization: Bearer <jwt_token>
```

### Compliance
Scraping is governed by configurable rules:
- robots.txt enforcement
- Per-domain rate limiting
- Domain allowlist
- Global kill-switch

### Rate Limits
- Search: 30 requests/minute
- Price Grid: 60 requests/minute
- Alert Creation: 10 requests/minute
            """,
            routes=app.routes,
        )

        # Security schemes
        openapi_schema["components"]["securitySchemes"] = {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Admin or merchant JWT token",
            },
            "apiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": "Merchant API key (for webhook verification)",
            },
        }

        # Tag descriptions
        openapi_schema["tags"] = [
            {
                "name": "Products",
                "description": "Product search, price grids, history, and AI insights",
                "externalDocs": {
                    "description": "Search algorithm details",
                    "url": "https://github.com/mukro/price_comparision_engine/blob/main/docs/search.md",
                },
            },
            {
                "name": "Price Drop Alerts",
                "description": "Subscribe to email alerts when prices drop below a target",
            },
            {
                "name": "Admin",
                "description": "Admin-only endpoints for moderation, compliance, and governance",
            },
            {
                "name": "B2B Merchant Pricing Engine",
                "description": "Dynamic repricing rules, simulations, and webhook management",
            },
        ]

        # Add server info
        openapi_schema["servers"] = [
            {"url": "http://localhost:8000", "description": "Local development"},
            {"url": "https://api.yourdomain.com", "description": "Production"},
        ]

        # Add external docs
        openapi_schema["externalDocs"] = {
            "description": "GitHub Repository",
            "url": "https://github.com/mukro/price_comparision_engine",
        }

        # Add contact info
        openapi_schema["info"]["contact"] = {
            "name": "API Support",
            "email": "support@yourdomain.com",
            "url": "https://yourdomain.com/support",
        }

        # Add license
        openapi_schema["info"]["license"] = {
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        }

        # Annotate each route with security requirements
        for path_data in openapi_schema.get("paths", {}).values():
            for operation in path_data.values():
                if isinstance(operation, dict):
                    # Admin endpoints
                    if operation.get("tags") and "Admin" in operation["tags"]:
                        operation["security"] = [{"bearerAuth": []}]
                    # Merchant endpoints
                    elif operation.get("tags") and "B2B Merchant Pricing Engine" in operation["tags"]:
                        operation["security"] = [{"bearerAuth": []}]
                    # Alert endpoints — public for creation, but could add auth later
                    elif operation.get("tags") and "Price Drop Alerts" in operation["tags"]:
                        operation["security"] = []  # Public

        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi
