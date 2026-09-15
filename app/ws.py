"""GGH-301 — minimal in-process WebSocket broadcast for live dashboard
updates. Connections are held in a plain in-memory dict, which is only
correct for a single web process (see Procfile) — scaling to multiple
workers/dynos would need a pub/sub layer (e.g. Redis) to fan broadcasts out
across processes. Messages are bare signals ("something changed, go
refetch"), not data payloads, so this stays a thin notification channel
rather than a second source of truth to keep in sync with the REST API.
"""

import uuid

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[uuid.UUID, set[WebSocket]] = {}

    async def connect(self, project_id: uuid.UUID, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(project_id, set()).add(websocket)

    def disconnect(self, project_id: uuid.UUID, websocket: WebSocket) -> None:
        connections = self._connections.get(project_id)
        if not connections:
            return
        connections.discard(websocket)
        if not connections:
            del self._connections[project_id]

    async def broadcast(self, project_id: uuid.UUID, message: dict) -> None:
        for websocket in list(self._connections.get(project_id, ())):
            try:
                await websocket.send_json(message)
            except Exception:
                # Best-effort: a dead socket here will also hit disconnect()
                # via its own receive loop erroring out; don't let one bad
                # connection break the broadcast to everyone else.
                pass


manager = ConnectionManager()
