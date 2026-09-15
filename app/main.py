import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import audit, auth, estimates, health, milestones, organizations, projects, users, ws
from app.core.config import settings
from app.middleware import SecurityHeadersMiddleware

logging.basicConfig(level=logging.INFO)

app = FastAPI(title=settings.APP_NAME)

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
app.include_router(ws.router)
