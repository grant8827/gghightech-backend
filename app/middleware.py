"""GGH-602 — baseline security response headers.

No Content-Security-Policy here: this API only ever serves JSON to the
actual frontend app, except for FastAPI's own /docs, /redoc, /openapi.json
(which load Swagger/ReDoc's CDN assets and would need a much looser policy
to keep working) — simplest correct behavior is to leave CSP to the
frontend, which serves the HTML that's actually rendered in a browser.
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.config import settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if settings.ENVIRONMENT != "development":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        return response
