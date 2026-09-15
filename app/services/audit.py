"""GGH-602 — audit trail for mutating actions.

record_audit_event() only stages the row (db.add) — it deliberately does not
commit. Call it right before the route's own db.commit() so the audit row
and the mutation it describes land in the same transaction: either both
persist or both roll back together.
"""

from typing import Optional, Union

import uuid

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.services.auth import AuthenticatedUser


def record_audit_event(
    db: Session,
    *,
    user: Optional[AuthenticatedUser],
    action: str,
    resource_type: str,
    resource_id: Optional[Union[uuid.UUID, str]] = None,
    org_id: Optional[uuid.UUID] = None,
    metadata: Optional[dict] = None,
) -> None:
    db.add(
        AuditLog(
            org_id=org_id,
            actor_id=(user.user_id or user.clerk_user_id) if user else None,
            actor_email=user.email if user else None,
            actor_role=user.role if user else "ANONYMOUS",
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            event_metadata=metadata,
        )
    )
