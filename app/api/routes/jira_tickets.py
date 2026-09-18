import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.jira_ticket import JiraTicket
from app.models.project import Project
from app.schemas.jira_ticket import JiraTicketCreate, JiraTicketOut
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, require_roles
from app.services.jira_service import JiraNotConfiguredError, JiraSyncError, create_issue

router = APIRouter(prefix="/api/v1/projects", tags=["jira-tickets"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("/{project_id}/jira-tickets", response_model=JiraTicketOut, status_code=201)
def create_jira_ticket(
    project_id: uuid.UUID,
    payload: JiraTicketCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> JiraTicket:
    """Writes a real ticket into Jira for developers to pick up. jira_tickets
    isn't RLS-protected (see the model docstring) so this is a plain
    commit/refresh, not commit_with_rls_refresh — that helper only matters
    for the four RLS-protected tables."""
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    if not project.jira_project_key:
        raise HTTPException(422, "Set a jira_project_key for this project first")

    try:
        result = create_issue(project.jira_project_key, payload.summary, payload.description, payload.issue_type)
    except JiraNotConfiguredError as exc:
        raise HTTPException(503, str(exc)) from exc
    except JiraSyncError as exc:
        raise HTTPException(422, str(exc)) from exc

    ticket = JiraTicket(
        project_id=project.id,
        org_id=project.org_id,
        jira_issue_key=result["key"],
        jira_url=result["url"],
        summary=payload.summary,
        description=payload.description,
        issue_type=payload.issue_type,
        created_by_email=user.email,
    )
    db.add(ticket)
    db.flush()
    record_audit_event(
        db,
        user=user,
        action="project.jira_ticket_create",
        resource_type="jira_ticket",
        resource_id=ticket.id,
        org_id=project.org_id,
        metadata={"jira_issue_key": result["key"], "summary": payload.summary},
    )
    db.commit()
    db.refresh(ticket)
    return ticket


@router.get("/{project_id}/jira-tickets", response_model=list[JiraTicketOut])
def list_jira_tickets(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> list[JiraTicket]:
    return (
        db.query(JiraTicket)
        .filter(JiraTicket.project_id == project_id)
        .order_by(JiraTicket.created_at.desc())
        .all()
    )
