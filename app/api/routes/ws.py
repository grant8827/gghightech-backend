"""GGH-301 — live dashboard updates. See app/ws.py for the connection
manager and its single-process caveat."""

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db.session import SessionLocal
from app.models.project import Project
from app.services.auth import resolve_authenticated_user, resolve_org_id
from app.ws import manager

router = APIRouter()


@router.websocket("/api/v1/ws/projects/{project_id}")
async def project_updates(project_id: uuid.UUID, websocket: WebSocket, token: str = "") -> None:
    """A browser WebSocket can't send an Authorization header, so the caller
    passes its bearer token as a query param instead: `?token=...`."""
    db = SessionLocal()
    try:
        try:
            user = await resolve_authenticated_user(token or None)
            caller_org_id = resolve_org_id(db, user)
        except Exception:
            await websocket.close(code=4401)
            return

        project = db.get(Project, project_id)
        if not project or (caller_org_id is not None and project.org_id != caller_org_id):
            await websocket.close(code=4404)
            return

        await manager.connect(project_id, websocket)
        try:
            while True:
                # We never expect messages from the client — this just keeps
                # the connection open and detects disconnects.
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            manager.disconnect(project_id, websocket)
    finally:
        db.close()
