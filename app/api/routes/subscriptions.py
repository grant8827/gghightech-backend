from typing import Optional

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.invoice import Invoice
from app.models.organization import Organization
from app.models.subscription_plan import SUBSCRIPTION_STATUSES, SubscriptionPlan
from app.schemas.invoice import InvoiceOut
from app.schemas.subscription_plan import SubscriptionPlanCreate, SubscriptionPlanOut, SubscriptionPlanUpdate
from app.services.audit import record_audit_event
from app.services.auth import AuthenticatedUser, commit_with_rls_refresh, require_roles
from app.services.stripe_service import (
    StripeIntegrationError,
    create_subscription_checkout_session,
)

router = APIRouter(prefix="/api/v1/subscriptions", tags=["subscriptions"])

_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("", response_model=SubscriptionPlanOut, status_code=201)
def create_subscription_plan(
    payload: SubscriptionPlanCreate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> SubscriptionPlan:
    """subscription_plans isn't RLS-protected (see the model docstring), so
    this is a plain commit/refresh."""
    if not db.get(Organization, payload.org_id):
        raise HTTPException(404, "Organization not found")

    plan = SubscriptionPlan(**payload.model_dump())
    db.add(plan)
    db.flush()
    record_audit_event(
        db,
        user=user,
        action="subscription.create",
        resource_type="subscription_plan",
        resource_id=plan.id,
        org_id=plan.org_id,
        metadata=payload.model_dump(mode="json"),
    )
    db.commit()
    db.refresh(plan)
    return plan


@router.get("", response_model=list[SubscriptionPlanOut])
def list_subscription_plans(
    org_id: Optional[uuid.UUID] = None,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> list[SubscriptionPlan]:
    query = db.query(SubscriptionPlan)
    if org_id:
        query = query.filter(SubscriptionPlan.org_id == org_id)
    return query.order_by(SubscriptionPlan.created_at.desc()).all()


@router.patch("/{plan_id}", response_model=SubscriptionPlanOut)
def update_subscription_plan(
    plan_id: uuid.UUID,
    payload: SubscriptionPlanUpdate,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> SubscriptionPlan:
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Subscription plan not found")

    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] not in SUBSCRIPTION_STATUSES:
        raise HTTPException(422, f"status must be one of {SUBSCRIPTION_STATUSES}")
    for field, value in changes.items():
        setattr(plan, field, value)

    record_audit_event(
        db,
        user=user,
        action="subscription.update",
        resource_type="subscription_plan",
        resource_id=plan.id,
        org_id=plan.org_id,
        metadata=changes,
    )
    db.commit()
    db.refresh(plan)
    return plan


@router.post("/{plan_id}/generate-invoice", response_model=InvoiceOut, status_code=201)
def generate_subscription_invoice(
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> Invoice:
    """Creates one real ad-hoc Invoice on demand — there's no scheduler
    behind this plan, see the model docstring. Invoice *is* RLS-protected,
    so unlike this route's own SubscriptionPlan bookkeeping just above,
    creating it needs commit_with_rls_refresh (staff route -> org_id=None
    bypass), same as every other invoice-creating route in this app."""
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Subscription plan not found")
    if plan.status != "ACTIVE":
        raise HTTPException(409, "This plan isn't active")
    if not plan.project_id:
        raise HTTPException(422, "This plan has no project to bill against")

    now = datetime.now(timezone.utc)
    invoice = Invoice(
        org_id=plan.org_id,
        project_id=plan.project_id,
        milestone_id=None,
        description=f"{plan.name} — {now:%B %Y}",
        amount=plan.amount,
        status="PENDING",
    )
    db.add(invoice)
    db.flush()
    plan.last_invoiced_at = now
    record_audit_event(
        db,
        user=user,
        action="subscription.generate_invoice",
        resource_type="invoice",
        resource_id=invoice.id,
        org_id=plan.org_id,
        metadata={"plan_id": str(plan.id), "amount": float(plan.amount)},
    )
    commit_with_rls_refresh(db, invoice, None)
    return invoice


@router.post("/{plan_id}/checkout")
def create_subscription_checkout(
    plan_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: AuthenticatedUser = Depends(require_roles(*_STAFF_ROLES)),
) -> dict:
    """Create a shareable Stripe-hosted enrollment link for a monthly plan."""
    plan = db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise HTTPException(404, "Subscription plan not found")
    if plan.status == "CANCELED":
        raise HTTPException(409, "Canceled plans cannot start Stripe Checkout")
    try:
        checkout_url = create_subscription_checkout_session(plan.id, float(plan.amount), plan.name)
    except StripeIntegrationError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"checkout_url": checkout_url}
