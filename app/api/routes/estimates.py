import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.estimate import Estimate
from app.schemas.estimate import EstimateCreate, EstimateOptions, EstimateOut, EstimatePreview
from app.services.audit import record_audit_event
from app.services.auth import require_roles
from app.services.email import send_lead_notification
from app.services.pdf import build_estimate_pdf
from app.services.pricing import (
    InvalidScopeError,
    apply_scope_adjustment,
    calculate_estimate,
    calculate_infrastructure,
    infrastructure_totals,
)
from app.services.scope_analysis import analyze_scope

router = APIRouter(prefix="/api/v1/estimates", tags=["estimates"])

# Staff can review the description, scope analysis, and operating-cost
# assumptions alongside the computed price.
_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("/preview", response_model=EstimatePreview)
def preview_estimate(payload: EstimateOptions) -> EstimatePreview:
    """Stateless calculation for live UI updates as the user toggles scope
    options (GGH-201) — no DB write, so it's safe to call on every change
    without spamming the estimates table with draft rows. Persisting happens
    only in POST /estimates, when the user actually submits/exports."""
    try:
        result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    infrastructure = calculate_infrastructure(payload.project_type, payload.features)
    monthly_min, monthly_max, first_year_min, first_year_max = infrastructure_totals(infrastructure)
    return EstimatePreview(
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


@router.post("", response_model=EstimateOut, status_code=201)
def create_estimate(payload: EstimateCreate, db: Session = Depends(get_db)) -> Estimate:
    try:
        base_result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    analysis = analyze_scope(
        payload.project_description, payload.project_type, payload.features, payload.design_tier
    )
    result = apply_scope_adjustment(base_result, analysis.adjustment_percent)
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

    estimate = Estimate(
        client_email=payload.client_email,
        client_phone=payload.client_phone,
        scope_configuration={
            "project_type": payload.project_type,
            "features": payload.features,
            "design_tier": payload.design_tier,
            "analysis": analysis.as_dict(),
            "infrastructure": infrastructure_data,
            "monthly_operating_min": monthly_min,
            "monthly_operating_max": monthly_max,
            "first_year_operating_min": first_year_min,
            "first_year_operating_max": first_year_max,
        },
        project_description=payload.project_description,
        calculated_min_price=result.price_min,
        calculated_max_price=result.price_max,
        estimated_weeks_min=result.weeks_min,
        estimated_weeks_max=result.weeks_max,
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
        send_lead_notification(
            estimate_id=str(estimate.id),
            client_email=contact,
            summary=f"{payload.project_type} / {payload.features} / {payload.design_tier} "
            f"-> ${result.price_min:,.0f}-${result.price_max:,.0f}, {result.weeks_min}-{result.weeks_max}wks"
            + (f" | client says: {payload.project_description}" if payload.project_description else ""),
        )

    return estimate


@router.get("", response_model=list[EstimateOut], dependencies=[Depends(require_roles(*_STAFF_ROLES))])
def list_estimates(db: Session = Depends(get_db)) -> list[Estimate]:
    """Estimate leads with the original brief and generated scope analysis."""
    return db.query(Estimate).order_by(Estimate.created_at.desc()).all()


@router.get("/{estimate_id}", response_model=EstimateOut)
def get_estimate(estimate_id: uuid.UUID, db: Session = Depends(get_db)) -> Estimate:
    estimate = db.get(Estimate, estimate_id)
    if not estimate:
        raise HTTPException(404, "Estimate not found")
    return estimate


@router.get("/{estimate_id}/pdf")
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
        client_email=estimate.client_email,
        client_phone=estimate.client_phone,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="gghightech-estimate-{estimate.id}.pdf"'},
    )
