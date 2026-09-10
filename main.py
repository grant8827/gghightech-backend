"""Entrypoint shim so `uvicorn main:app` keeps working — the real app now
lives in app/main.py, structured into routers/models/schemas/services."""

from app.main import app

__all__ = ["app"]
