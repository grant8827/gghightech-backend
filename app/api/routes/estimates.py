import uuid
from typing import Optional

import jwt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.models.estimate import Estimate
from app.schemas.estimate import EstimateCreate, EstimateCreated, EstimateOptions, EstimateOut, EstimatePreview
from app.services.audit import record_audit_event
from app.services.auth import get_current_user, require_roles
from app.services.email import send_lead_notification
from app.services.local_auth import create_estimate_pdf_token, decode_estimate_pdf_token
from app.services.pdf import build_estimate_pdf
from app.services.pricing import (
    InvalidScopeError,
    apply_scope_adjustment,
    calculate_estimate,
    calculate_infrastructure,
    calculate_maintenance,
    calculate_update_estimate,
    infrastructure_totals,
)
from app.services.rate_limit import Rule, rate_limit
from app.services.scope_analysis import analyze_scope

router = APIRouter(prefix="/api/v1/estimates", tags=["estimates"])

# Staff can review the description, scope analysis, and operating-cost
# assumptions alongside the computed price.
_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


def _create_rules() -> list[Rule]:
    return [
        (settings.ESTIMATE_CREATE_LIMIT_PER_10_MIN, 10 * 60),
        (settings.ESTIMATE_CREATE_LIMIT_PER_DAY, 24 * 60 * 60),
    ]


def _preview_rules() -> list[Rule]:
    return [(settings.ESTIMATE_PREVIEW_LIMIT_PER_MIN, 60)]


@router.post(
    "/preview",
    response_model=EstimatePreview,
    dependencies=[Depends(rate_limit("estimate-preview", _preview_rules))],
)
def preview_estimate(payload: EstimateOptions) -> EstimatePreview:
    """Stateless calculation for live UI updates as the user toggles scope
    options (GGH-201) — no DB write, so it's safe to call on every change
    without spamming the estimates table with draft rows. Persisting happens
    only in POST /estimates, when the user actually submits/exports."""
    try:
        if payload.request_type == "MAINTENANCE":
            # A monthly retainer, not a one-time build — see
            # app/services/pricing.py's MAINTENANCE_PLANS. No build price/
            # timeline and no new infrastructure (it's already running).
            plan = calculate_maintenance(payload.project_type)
            return EstimatePreview(
                request_type=payload.request_type,
                calculated_min_price=0,
                calculated_max_price=0,
                estimated_weeks_min=0,
                estimated_weeks_max=0,
                infrastructure=[],
                monthly_operating_min=0,
                monthly_operating_max=0,
                first_year_operating_min=0,
                first_year_operating_max=0,
                maintenance_monthly_min=plan.monthly_min,
                maintenance_monthly_max=plan.monthly_max,
                maintenance_hours_per_week_min=plan.hours_per_week_min,
                maintenance_hours_per_week_max=plan.hours_per_week_max,
            )

        if payload.request_type == "UPDATE":
            result = calculate_update_estimate(payload.features, payload.design_tier)
        else:
            result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    infrastructure = calculate_infrastructure(payload.project_type, payload.features)
    monthly_min, monthly_max, first_year_min, first_year_max = infrastructure_totals(infrastructure)
    return EstimatePreview(
        request_type=payload.request_type,
        calculated_min_price=result.price_min,
        calculated_max_price=result.price_max,
        estimated_weeks_min=result.weeks_min,
        estimated_weeks_max=result.weeks_max,
        infrastructure=[item.__dict__ for item in infrastructure],
        monthly_operating_min=monthly_min,
        monthly_operating_max=monthly_max,
        first_year_operating_min=first_year_min,
        first_year_operating_max=first_year_max,
    )


@router.post(
    "",
    response_model=EstimateCreated,
    status_code=201,
    # Public, writes to the DB, and may trigger a paid AI call — limited
    # per client here, and globally by app/services/ai_budget.py.
    dependencies=[Depends(rate_limit("estimate-create", _create_rules))],
)
def create_estimate(payload: EstimateCreate, db: Session = Depends(get_db)) -> EstimateCreated:
    # Still analyzed for all three request types — useful staff context
    # (complexity, detected requirements, risks) even on an update/
    # maintenance inquiry — but its price/timeline adjustment_percent is
    # only ever applied below for NEW/UPDATE, never MAINTENANCE.
    analysis = analyze_scope(
        payload.project_description,
        payload.request_type,
        payload.project_type,
        payload.features,
        payload.design_tier,
    )

    scope_extra: dict = {}
    try:
        if payload.request_type == "MAINTENANCE":
            plan = calculate_maintenance(payload.project_type)
            result_min_price = result_max_price = 0.0
            result_weeks_min = result_weeks_max = 0
            infrastructure_data: list[dict] = []
            monthly_min = monthly_max = first_year_min = first_year_max = 0.0
            scope_extra = {
                "maintenance_monthly_min": analysis.recommended_monthly_min if analysis.source == "openai" else plan.monthly_min,
                "maintenance_monthly_max": analysis.recommended_monthly_max if analysis.source == "openai" else plan.monthly_max,
                "maintenance_hours_per_week_min": analysis.recommended_hours_per_week_min if analysis.source == "openai" else plan.hours_per_week_min,
                "maintenance_hours_per_week_max": analysis.recommended_hours_per_week_max if analysis.source == "openai" else plan.hours_per_week_max,
            }
        else:
            if payload.request_type == "UPDATE":
                base_result = calculate_update_estimate(payload.features, payload.design_tier)
            else:
                base_result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
            if analysis.source == "openai":
                result_min_price = analysis.recommended_price_min
                result_max_price = analysis.recommended_price_max
                result_weeks_min = analysis.recommended_weeks_min
                result_weeks_max = analysis.recommended_weeks_max
            else:
                result = apply_scope_adjustment(base_result, analysis.adjustment_percent)
                result_min_price, result_max_price = result.price_min, result.price_max
                result_weeks_min, result_weeks_max = result.weeks_min, result.weeks_max

            infrastructure = calculate_infrastructure(
                payload.project_type, payload.features, payload.project_description
            )
            monthly_min, monthly_max, first_year_min, first_year_max = infrastructure_totals(infrastructure)
            infrastructure_data = [
                {
                    "name": item.name,
                    "monthly_min": item.monthly_min,
                    "monthly_max": item.monthly_max,
                    "annual_min": item.annual_min,
                    "annual_max": item.annual_max,
                    "note": item.note,
                }
                for item in infrastructure
            ]
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    estimate = Estimate(
        client_email=payload.client_email,
        client_phone=payload.client_phone,
        scope_configuration={
            "request_type": payload.request_type,
            "project_type": payload.project_type,
            "features": payload.features,
            "design_tier": payload.design_tier,
            "analysis": analysis.as_dict(),
            "infrastructure": infrastructure_data,
            "monthly_operating_min": monthly_min,
            "monthly_operating_max": monthly_max,
            "first_year_operating_min": first_year_min,
            "first_year_operating_max": first_year_max,
            **scope_extra,
        },
        project_description=payload.project_description,
        calculated_min_price=result_min_price,
        calculated_max_price=result_max_price,
        estimated_weeks_min=result_weeks_min,
        estimated_weeks_max=result_weeks_max,
        status="SUBMITTED" if (payload.client_email or payload.client_phone) else "DRAFT",
    )
    db.add(estimate)
    db.flush()  # assigns estimate.id so the audit row below can reference it
    record_audit_event(
        db,
        user=None,  # public lead-capture endpoint, no signed-in caller
        action="estimate.create",
        resource_type="estimate",
        resource_id=estimate.id,
        metadata={"project_type": payload.project_type, "features": payload.features},
    )
    db.commit()
    db.refresh(estimate)

    if payload.client_email or payload.client_phone:
        contact = payload.client_email or "(no email given)"
        if payload.client_phone:
            contact += f" / {payload.client_phone}"
        if payload.request_type == "MAINTENANCE":
            price_summary = f"${scope_extra['maintenance_monthly_min']:,.0f}-${scope_extra['maintenance_monthly_max']:,.0f}/mo"
        else:
            price_summary = f"${result_min_price:,.0f}-${result_max_price:,.0f}, {result_weeks_min}-{result_weeks_max}wks"
        send_lead_notification(
            estimate_id=str(estimate.id),
            client_email=contact,
            summary=f"{payload.request_type} / {payload.project_type} / {payload.features} / {payload.design_tier} "
            f"-> {price_summary}"
            + (f" | client says: {payload.project_description}" if payload.project_description else ""),
        )

    # The only way an anonymous submitter can fetch this estimate's PDF
    # afterwards — reading an estimate is otherwise staff-only (see
    # _authorize_estimate_pdf below).
    response = EstimateCreated.model_validate(estimate, from_attributes=True)
    if settings.SECRET_KEY:
        response.pdf_token = create_estimate_pdf_token(str(estimate.id))
    return response


@router.get("", response_model=list[EstimateOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_estimates(db: Session = Depends(get_db)) -> list[Estimate]:
    """Estimate leads with the original brief and generated scope analysis."""
    return db.query(Estimate).order_by(Estimate.created_at.desc()).all()


@router.get("/{estimate_id}", response_model=EstimateOut, dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def get_estimate(estimate_id: uuid.UUID, db: Session = Depends(get_db)) -> Estimate:
    estimate = db.get(Estimate, estimate_id)
    if not estimate:
        raise HTTPException(404, "Estimate not found")
    return estimate


async def _authorize_estimate_pdf(
    estimate_id: uuid.UUID,
    x_estimate_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
    x_dev_user_role: Optional[str] = Header(default=None),
    x_dev_user_email: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
) -> None:
    """An estimate's PDF carries the lead's contact details and brief, so
    it is released to exactly two kinds of caller: the visitor who just
    submitted it (the short-lived X-Estimate-Token from POST /estimates,
    valid for that one estimate only), or signed-in staff. Runs before any
    DB lookup, so an unauthorized caller can't tell whether an id exists.

    The token is a header, not a query parameter, to keep it out of URLs,
    browser history, and access logs."""
    if x_estimate_token:
        if settings.SECRET_KEY:
            try:
                claims = decode_estimate_pdf_token(x_estimate_token)
            except jwt.PyJWTError:
                claims = None
            if claims and claims["sub"] == str(estimate_id):
                return
        raise HTTPException(401, "This download link is invalid or has expired")

    user = await get_current_user(authorization, x_dev_user_role, x_dev_user_email, db)
    if user.role not in _STAFF_ROLES:
        raise HTTPException(403, f"Requires one of roles: {_STAFF_ROLES}")


@router.get("/{estimate_id}/pdf", dependencies=[Depends(_authorize_estimate_pdf)])
def get_estimate_pdf(estimate_id: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    estimate = db.get(Estimate, estimate_id)
    if not estimate:
        raise HTTPException(404, "Estimate not found")

    scope = estimate.scope_configuration
    pdf_bytes = build_estimate_pdf(
        estimate_id=estimate.id,
        project_type=scope["project_type"],
        features=scope["features"],
        design_tier=scope["design_tier"],
        request_type=scope.get("request_type", "NEW"),
        price_min=float(estimate.calculated_min_price),
        price_max=float(estimate.calculated_max_price),
        weeks_min=estimate.estimated_weeks_min,
        weeks_max=estimate.estimated_weeks_max,
        project_description=estimate.project_description,
        analysis=scope.get("analysis"),
        infrastructure=scope.get("infrastructure", []),
        monthly_operating_min=scope.get("monthly_operating_min", 0),
        monthly_operating_max=scope.get("monthly_operating_max", 0),
        first_year_operating_min=scope.get("first_year_operating_min", 0),
        first_year_operating_max=scope.get("first_year_operating_max", 0),
        maintenance_monthly_min=scope.get("maintenance_monthly_min", 0),
        maintenance_monthly_max=scope.get("maintenance_monthly_max", 0),
        maintenance_hours_per_week_min=scope.get("maintenance_hours_per_week_min", 0),
        maintenance_hours_per_week_max=scope.get("maintenance_hours_per_week_max", 0),
        client_email=estimate.client_email,
        client_phone=estimate.client_phone,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="gghightech-estimate-{estimate.id}.pdf"',
            "Cache-Control": "private, no-store",
        },
    )
