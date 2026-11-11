import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import (
    audit,
    auth,
    email_server,
    estimates,
    health,
    instant_payments,
    invoices,
    jira_tickets,
    messages,
    milestones,
    organizations,
    projects,
    reviews,
    subscriptions,
    stripe_webhooks,
    updates,
    users,
    ws,
)
from app.core.config import settings
from app.middleware import SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO)

# The interactive docs and OpenAPI schema map every endpoint — useful
# locally, nothing the public needs (audit SEC-12).
_docs = settings.is_development
app = FastAPI(
    title=settings.APP_NAME,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(estimates.router)
app.include_router(organizations.router)
app.include_router(projects.router)
app.include_router(milestones.router)
app.include_router(users.router)
app.include_router(audit.router)
app.include_router(email_server.router)
app.include_router(invoices.router)
app.include_router(instant_payments.router)
app.include_router(updates.router)
app.include_router(jira_tickets.router)
app.include_router(subscriptions.router)
app.include_router(reviews.router)
app.include_router(messages.router)
app.include_router(stripe_webhooks.router)
app.include_router(ws.router)
