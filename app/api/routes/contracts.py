"""Project contracts: staff draft and send them from the admin Invoicing /
Contracts tab; the client's CLIENT_ADMIN reads and e-signs them in the
portal. See app/models/contract.py for the lifecycle.

Clients never see DRAFT contracts. Every list/get is org-scoped for client
callers (get_current_user_org_id), on top of the table's row-level security.
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.contract import Contract
from app.models.organization import Organization
from app.models.project import Project
from app.models.user import User
from app.schemas.contract import (
    ContractCreate,
    ContractDefaults,
    ContractOut,
    ContractSent,
    ContractSign,
    ContractUpdate,
)
from app.services.audit import record_audit_event
from app.services.auth import (
    AuthenticatedUser,
    commit_with_rls_refresh,
    get_current_user,
    get_current_user_org_id,
    require_roles,
)
from app.services.documents import ContractDoc, build_contract_document
from app.services.email import send_contract_email, send_contract_signed_email
from app.services.rate_limit import client_ip
from app.ws import manager

router = APIRouter(prefix="/api/v1/contracts", tags=["contracts"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")

DEFAULT_PAYMENT_TERMS = (
    "Invoices are due within 14 days of the invoice date. Payments can be made by card through the client "
    "dashboard or by bank transfer. Work on the next phase begins once the previous payment has been received."
)

# A plain-language starting point that staff edit per project. It is not
# legal advice: have it reviewed by a lawyer before relying on it.
DEFAULT_TERMS = """1. Changes to scope. Work not described in this Agreement is a change request. Provider will quote it in writing before starting, and it is billed in addition to the contract value.

2. Client responsibilities. Client will provide content, access, feedback and approvals on time. Delays on Client's side may move the timeline accordingly.

3. Approvals. Each milestone is considered accepted when Client approves it in the client dashboard, or 7 days after delivery if no issues are reported.

4. Ownership. When all fees are paid in full, Client owns the final deliverables created for this project. Provider keeps ownership of its pre-existing tools, code libraries and know-how, and grants Client a perpetual licence to use them as part of the deliverables.

5. Third-party services. Hosting, domains, software licences and other third-party services are billed at cost or paid directly by Client, and are subject to those providers' terms.

6. Confidentiality. Each party keeps the other's non-public information confidential and uses it only for this project.

7. Portfolio. Provider may show the finished work in its portfolio unless Client asks otherwise in writing.

8. Warranty. Provider will fix defects in the deliverables reported within 30 days of launch at no charge. After that, support is available under a maintenance plan.

9. Limitation of liability. Each party's total liability under this Agreement is limited to the fees paid under it. Neither party is liable for indirect or consequential losses.

10. Termination. Either party may end this Agreement with 14 days' written notice. Client pays for work completed up to the termination date.

11. Entire agreement. This Agreement is the full agreement between the parties for this project. Changes must be agreed in writing by both parties."""


def _staff_name(db: Session, user: AuthenticatedUser) -> str:
    if user.user_id:
        try:
            found = db.get(User, uuid.UUID(str(user.user_id)))
        except ValueError:
            found = None
        if found and found.full_name:
            return found.full_name
    return user.email or "GG HighTech"


def _content_hash(c: Contract) -> str:
    """Fingerprint of everything the client agrees to — stored at signing."""
    content = {
        "number": c.contract_number,
        "title": c.title,
        "client_name": c.client_name,
        "client_email": c.client_email,
        "effective_date": c.effective_date.isoformat() if c.effective_date else None,
        "scope": c.scope,
        "deliverables": c.deliverables,
        "timeline": c.timeline,
        "payment_schedule": c.payment_schedule,
        "total_value": f"{float(c.total_value):.2f}",
        "payment_terms": c.payment_terms,
        "terms": c.terms,
    }
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _total(schedule: list[dict], fallback: float) -> float:
    return round(sum(float(p["amount"]) for p in schedule), 2) if schedule else round(fallback, 2)


def _pdf(db: Session, c: Contract) -> bytes:
    project = db.get(Project, c.project_id)
    org = db.get(Organization, c.org_id)
    return build_contract_document(
        ContractDoc(
            contract_id=c.id,
            number=c.contract_number or str(c.id)[:8].upper(),
            title=c.title,
            status=c.status,
            project_title=project.title if project else "Project",
            client_org=org.name if org else c.client_name,
            client_name=c.client_name,
            client_email=c.client_email,
            effective_date=c.effective_date,
            scope=c.scope,
            deliverables=c.deliverables or [],
            timeline=c.timeline,
            payment_schedule=c.payment_schedule or [],
            total_value=float(c.total_value),
            payment_terms=c.payment_terms,
            terms=c.terms,
            provider_signer_name=c.provider_signer_name,
            sent_at=c.sent_at,
            signer_name=c.signer_name,
            signer_email=c.signer_email,
            signed_at=c.signed_at,
            signer_ip=c.signer_ip,
            signed_content_hash=c.signed_content_hash,
        )
    )


def _filename(c: Contract) -> str:
    return f"gghightech-agreement-{c.contract_number or c.id}.pdf"


def _get_scoped(db: Session, contract_id: uuid.UUID, caller_org_id: Optional[uuid.UUID]) -> Contract:
    c = db.get(Contract, contract_id)
    # Clients never see drafts — a draft is a 404 to them, same as another org's.
    if not c or (caller_org_id is not None and (c.org_id != caller_org_id or c.status == "DRAFT")):
        raise HTTPException(404, "Contract not found")
    return c


def _get_staff(db: Session, contract_id: uuid.UUID) -> Contract:
    c = db.get(Contract, contract_id)
    if not c:
        raise HTTPException(404, "Contract not found")
    return c


@router.get("/defaults", response_model=ContractDefaults, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def contract_defaults() -> dict:
    """Starting text for a new contract's terms and payment terms."""
    return {"terms": DEFAULT_TERMS, "payment_terms": DEFAULT_PAYMENT_TERMS}


@router.get("", response_model=list[ContractOut])
def list_contracts(
    project_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> list[Contract]:
    query = db.query(Contract)
    if caller_org_id is not None:
        query = query.filter(Contract.org_id == caller_org_id, Contract.status != "DRAFT")
    if project_id:
        query = query.filter(Contract.project_id == project_id)
    return query.order_by(Contract.created_at.desc()).all()


@router.post("", response_model=ContractOut, status_code=201)
async def create_contract(
    payload: ContractCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Contract:
    project = db.get(Project, payload.project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    data = payload.model_dump(exclude={"project_id"})
    data["total_value"] = _total(data["payment_schedule"], data["total_value"])
    contract = Contract(
        org_id=project.org_id,
        project_id=project.id,
        status="DRAFT",
        created_by=_staff_name(db, user),
        **data,
    )
    db.add(contract)
    db.flush()
    record_audit_event(
        db,
        user=user,
        action="contract.create",
        resource_type="contract",
        resource_id=contract.id,
        org_id=contract.org_id,
        metadata={"title": contract.title, "total_value": float(contract.total_value)},
    )
    commit_with_rls_refresh(db, contract, None)
    return contract


@router.get("/{contract_id}", response_model=ContractOut)
def get_contract(
    contract_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Contract:
    return _get_scoped(db, contract_id, caller_org_id)


@router.patch("/{contract_id}", response_model=ContractOut)
async def update_contract(
    contract_id: uuid.UUID,
    payload: ContractUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Contract:
    """Drafts and sent-but-unsigned contracts only. Editing a SENT contract
    returns it to DRAFT: the client must get the new version before they
    can sign."""
    c = _get_staff(db, contract_id)
    if c.status not in ("DRAFT", "SENT"):
        raise HTTPException(409, f"A {c.status.lower()} contract can't be edited")
    changes = payload.model_dump(exclude_unset=True)
    for required in ("title", "client_name", "scope", "terms"):
        if required in changes and changes[required] is None:
            raise HTTPException(422, f"{required} can't be empty")
    for name, value in changes.items():
        setattr(c, name, value)
    if "payment_schedule" in changes or "total_value" in changes:
        c.total_value = _total(c.payment_schedule or [], float(changes.get("total_value", c.total_value) or 0))
    was_sent = c.status == "SENT"
    if was_sent:
        c.status = "DRAFT"
    c.updated_at = datetime.now(timezone.utc)
    record_audit_event(
        db,
        user=user,
        action="contract.update",
        resource_type="contract",
        resource_id=c.id,
        org_id=c.org_id,
        metadata={"fields": sorted(changes), "returned_to_draft": was_sent},
    )
    commit_with_rls_refresh(db, c, None)
    await manager.broadcast(c.project_id, {"type": "contract_updated"})
    return c


@router.post("/{contract_id}/send", response_model=ContractSent)
async def send_contract(
    contract_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> dict:
    """Mark the contract ready to sign and email the client a copy with a
    link to the portal. Callable again to resend."""
    c = _get_staff(db, contract_id)
    if c.status not in ("DRAFT", "SENT"):
        raise HTTPException(409, f"A {c.status.lower()} contract can't be sent")
    if not c.client_email:
        raise HTTPException(422, "Add the client's email to this contract first")
    c.status = "SENT"
    c.sent_at = datetime.now(timezone.utc)
    c.provider_signer_name = _staff_name(db, user)
    db.flush()
    sent = send_contract_email(
        to=c.client_email,
        name=c.client_name,
        number=c.contract_number or "",
        title=c.title,
        portal_url=f"{settings.FRONTEND_URL.rstrip('/')}/portal?tab=contracts",
        pdf=(_filename(c), _pdf(db, c)),
    )
    record_audit_event(
        db,
        user=user,
        action="contract.send",
        resource_type="contract",
        resource_id=c.id,
        org_id=c.org_id,
        metadata={"email_sent": sent},
    )
    commit_with_rls_refresh(db, c, None)
    await manager.broadcast(c.project_id, {"type": "contract_updated"})
    return {"contract": c, "email_sent": sent}


@router.post("/{contract_id}/sign", response_model=ContractOut)
async def sign_contract(
    contract_id: uuid.UUID,
    payload: ContractSign,
    request: Request,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(get_current_user),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Contract:
    """The client's CLIENT_ADMIN e-signs: typed full name + an explicit
    "I agree". Records who, when, from where, and a fingerprint of exactly
    what they agreed to."""
    if user.role != "CLIENT_ADMIN":
        raise HTTPException(403, "Only your organization's account admin can sign contracts")
    c = _get_scoped(db, contract_id, caller_org_id)
    if c.status == "SIGNED":
        raise HTTPException(409, "This contract is already signed")
    if c.status != "SENT":
        raise HTTPException(409, "This contract isn't ready to sign")

    c.status = "SIGNED"
    c.signed_at = datetime.now(timezone.utc)
    c.signer_name = payload.signer_name.strip()
    c.signer_email = user.email
    c.signer_ip = client_ip(request)[:64]
    c.signed_content_hash = _content_hash(c)
    record_audit_event(
        db,
        user=user,
        action="contract.sign",
        resource_type="contract",
        resource_id=c.id,
        org_id=c.org_id,
        metadata={"signer_name": c.signer_name, "content_hash": c.signed_content_hash},
    )
    commit_with_rls_refresh(db, c, caller_org_id)

    signed_pdf = (_filename(c), _pdf(db, c))
    for to in {addr for addr in (c.client_email, user.email, settings.LEAD_NOTIFICATION_EMAIL) if addr}:
        send_contract_signed_email(to, c.contract_number or "", c.title, c.signer_name, signed_pdf)
    await manager.broadcast(c.project_id, {"type": "contract_signed"})
    return c


@router.post("/{contract_id}/void", response_model=ContractOut)
async def void_contract(
    contract_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Contract:
    c = _get_staff(db, contract_id)
    if c.status == "VOID":
        raise HTTPException(409, "This contract is already void")
    previous = c.status
    c.status = "VOID"
    c.voided_at = datetime.now(timezone.utc)
    record_audit_event(
        db,
        user=user,
        action="contract.void",
        resource_type="contract",
        resource_id=c.id,
        org_id=c.org_id,
        metadata={"previous_status": previous},
    )
    commit_with_rls_refresh(db, c, None)
    await manager.broadcast(c.project_id, {"type": "contract_updated"})
    return c


@router.delete("/{contract_id}", status_code=204)
def delete_contract(
    contract_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> None:
    """Drafts only — anything the client has seen is voided instead."""
    c = _get_staff(db, contract_id)
    if c.status != "DRAFT":
        raise HTTPException(409, "Only drafts can be deleted; void a sent or signed contract instead")
    record_audit_event(
        db,
        user=user,
        action="contract.delete",
        resource_type="contract",
        resource_id=c.id,
        org_id=c.org_id,
        metadata={"title": c.title},
    )
    db.delete(c)
    db.commit()


@router.get("/{contract_id}/pdf")
def get_contract_pdf(
    contract_id: uuid.UUID,
    db: Session = Depends(get_db),
    caller_org_id: Optional[uuid.UUID] = Depends(get_current_user_org_id),
) -> Response:
    c = _get_scoped(db, contract_id, caller_org_id)
    return Response(
        content=_pdf(db, c),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{_filename(c)}"'},
    )
