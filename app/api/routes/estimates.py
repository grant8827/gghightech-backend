import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.estimate import Estimate
from app.schemas.estimate import EstimateCreate, EstimateOut, EstimatePreview
from app.services.auth import require_roles
from app.services.email import send_lead_notification
from app.services.pdf import build_estimate_pdf
from app.services.pricing import InvalidScopeError, calculate_estimate

router = APIRouter(prefix="/api/v1/estimates", tags=["estimates"])

# Staff review the free-text description against the toggle-based price —
# same roles that can manage projects/organizations.
_STAFF_ROLES = ("SUPER_ADMIN", "PROJECT_MANAGER", "LEAD_ENGINEER")


@router.post("/preview", response_model=EstimatePreview)
def preview_estimate(payload: EstimateCreate) -> EstimatePreview:
    """Stateless calculation for live UI updates as the user toggles scope
    options (GGH-201) — no DB write, so it's safe to call on every change
    without spamming the estimates table with draft rows. Persisting happens
    only in POST /estimates, when the user actually submits/exports."""
    try:
        result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    return EstimatePreview(
        calculated_min_price=result.price_min,
        calculated_max_price=result.price_max,
        estimated_weeks_min=result.weeks_min,
        estimated_weeks_max=result.weeks_max,
    )


@router.post("", response_model=EstimateOut, status_code=201)
def create_estimate(payload: EstimateCreate, db: Session = Depends(get_db)) -> Estimate:
    try:
        result = calculate_estimate(payload.project_type, payload.features, payload.design_tier)
    except InvalidScopeError as exc:
        raise HTTPException(422, str(exc)) from exc

    estimate = Estimate(
        client_email=payload.client_email,
        client_phone=payload.client_phone,
        scope_configuration={
            "project_type": payload.project_type,
            "features": payload.features,
            "design_tier": payload.design_tier,
        },
        project_description=payload.project_description,
        calculated_min_price=result.price_min,
        calculated_max_price=result.price_max,
        estimated_weeks_min=result.weeks_min,
        estimated_weeks_max=result.weeks_max,
        status="SUBMITTED" if (payload.client_email or payload.client_phone) else "DRAFT",
    )
    db.add(estimate)
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
    """Leads list for staff (GGH-202) — lets someone actually read the
    free-text project_description and sanity-check it against the
    toggle-based price before following up."""
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
        client_email=estimate.client_email,
        client_phone=estimate.client_phone,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="gghightech-estimate-{estimate.id}.pdf"'},
    )
