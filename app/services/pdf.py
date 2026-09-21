"""GGH-202 — technical proposal PDF export for a computed estimate.
Also builds milestone-approval invoice PDFs (app/api/routes/invoices.py)."""

import uuid
from datetime import datetime
from typing import Optional

from fpdf import FPDF

BRAND_COLOR = (17, 24, 39)  # near-black, matches the dark hero theme in GGH-101
ACCENT_COLOR = (232, 184, 75)  # gold, sampled from the logo — see frontend/app/globals.css


def _latin1_safe(text: str) -> str:
    """The core Helvetica font fpdf2 uses here only supports latin-1. Client
    free-text (project_description) is unconstrained input, so anything
    outside that range (emoji, non-Latin scripts) gets replaced rather than
    raising and failing the whole PDF export."""
    return text.encode("latin-1", errors="replace").decode("latin-1")


_REQUEST_TYPE_LABELS = {"NEW": "New Build", "UPDATE": "Project Update", "MAINTENANCE": "Maintenance Plan"}
_REQUEST_TYPE_FEATURES_LABEL = {"UPDATE": "Features to update", "MAINTENANCE": "Features in use today"}


def build_estimate_pdf(
    estimate_id: uuid.UUID,
    project_type: str,
    features: list[str],
    design_tier: str,
    price_min: float,
    price_max: float,
    weeks_min: int,
    weeks_max: int,
    request_type: str = "NEW",
    project_description: Optional[str] = None,
    client_email: Optional[str] = None,
    client_phone: Optional[str] = None,
    analysis: Optional[dict] = None,
    infrastructure: Optional[list[dict]] = None,
    monthly_operating_min: float = 0,
    monthly_operating_max: float = 0,
    first_year_operating_min: float = 0,
    first_year_operating_max: float = 0,
    maintenance_monthly_min: float = 0,
    maintenance_monthly_max: float = 0,
    maintenance_hours_per_week_min: int = 0,
    maintenance_hours_per_week_max: int = 0,
) -> bytes:
    pdf = FPDF(format="Letter")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*BRAND_COLOR)
    pdf.cell(0, 12, "GG HighTech", ln=True)

    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 8, _REQUEST_TYPE_LABELS.get(request_type, "Technical Scope Proposal"), ln=True)
    pdf.ln(4)

    pdf.set_draw_color(*ACCENT_COLOR)
    pdf.set_line_width(0.8)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(8)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Scope", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Project type: {project_type.replace('_', ' ').title()}", ln=True)
    if request_type != "MAINTENANCE":
        pdf.cell(0, 7, f"Design tier: {design_tier.replace('_', ' ').title()}", ln=True)
    features_label = ", ".join(f.replace("_", " ").title() for f in features) or "None"
    pdf.cell(0, 7, f"{_REQUEST_TYPE_FEATURES_LABEL.get(request_type, 'Features')}: {features_label}", ln=True)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Estimate", ln=True)
    pdf.set_font("Helvetica", "", 11)
    if request_type == "MAINTENANCE":
        pdf.cell(
            0,
            7,
            f"Monthly retainer: ${maintenance_monthly_min:,.0f} - ${maintenance_monthly_max:,.0f}",
            ln=True,
        )
        pdf.cell(
            0,
            7,
            f"Estimated involvement: {maintenance_hours_per_week_min} - {maintenance_hours_per_week_max} hrs/week",
            ln=True,
        )
    else:
        pdf.cell(0, 7, f"Budget range: ${price_min:,.0f} - ${price_max:,.0f}", ln=True)
        pdf.cell(0, 7, f"Timeline: {weeks_min} - {weeks_max} weeks", ln=True)
    pdf.ln(10)

    if infrastructure:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Estimated Operating Costs", ln=True)
        pdf.set_font("Helvetica", "", 10)
        for item in infrastructure:
            monthly = f"${item['monthly_min']:,.0f}-${item['monthly_max']:,.0f}/mo" if item["monthly_max"] else ""
            annual = f"${item['annual_min']:,.0f}-${item['annual_max']:,.0f}/yr" if item["annual_max"] else ""
            separator = " + " if monthly and annual else ""
            pdf.cell(0, 6, _latin1_safe(f"{item['name']}: {monthly}{separator}{annual}"), ln=True)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(0, 7, f"Monthly total: ${monthly_operating_min:,.0f}-${monthly_operating_max:,.0f}", ln=True)
        pdf.cell(0, 7, f"Estimated first year: ${first_year_operating_min:,.0f}-${first_year_operating_max:,.0f}", ln=True)
        pdf.ln(6)

    if analysis:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Scope Analysis", ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 6, _latin1_safe(analysis.get("summary", "")))
        pdf.cell(0, 6, f"Complexity: {analysis.get('complexity', 'standard').title()}", ln=True)
        pdf.cell(0, 6, f"Scope adjustment: {analysis.get('adjustment_percent', 0)}%", ln=True)
        pdf.multi_cell(0, 6, _latin1_safe(analysis.get("market_comparison", "")))
        pdf.ln(6)

    if client_email or client_phone:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "Contact", ln=True)
        pdf.set_font("Helvetica", "", 11)
        if client_email:
            pdf.cell(0, 7, f"Email: {client_email}", ln=True)
        if client_phone:
            pdf.cell(0, 7, f"Phone: {client_phone}", ln=True)
        pdf.ln(6)

    if project_description:
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "In the Client's Words", ln=True)
        pdf.set_font("Helvetica", "", 11)
        pdf.multi_cell(0, 6, _latin1_safe(project_description))
        pdf.ln(6)

    pdf.set_font("Helvetica", "I", 9)
    pdf.set_text_color(130, 130, 130)
    pdf.multi_cell(
        0,
        5,
        "This is an automated, non-binding estimate generated from the choices made in the "
        "GG HighTech interactive scope estimator. A GG HighTech engineer will follow up to "
        "confirm final scope and pricing.",
    )
    pdf.ln(4)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 5, f"Estimate reference: {estimate_id}", ln=True)

    return bytes(pdf.output())


def build_invoice_pdf(
    invoice_id: uuid.UUID,
    project_title: str,
    billed_for: str,
    amount: float,
    status: str,
    created_at: datetime,
    paid_at: Optional[datetime] = None,
) -> bytes:
    """billed_for is a milestone's title for milestone-driven invoices, or
    the invoice's own description for ad-hoc ones (see app/models/invoice.py)."""
    pdf = FPDF(format="Letter")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*BRAND_COLOR)
    pdf.cell(0, 12, "GG HighTech", ln=True)

    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 8, "Invoice", ln=True)
    pdf.ln(4)

    pdf.set_draw_color(*ACCENT_COLOR)
    pdf.set_line_width(0.8)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(8)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Billed for", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Project: {_latin1_safe(project_title)}", ln=True)
    pdf.cell(0, 7, f"For: {_latin1_safe(billed_for)}", ln=True)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Amount", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"${amount:,.2f}", ln=True)
    pdf.cell(0, 7, f"Status: {status}", ln=True)
    pdf.cell(0, 7, f"Issued: {created_at.strftime('%Y-%m-%d')}", ln=True)
    if paid_at:
        pdf.cell(0, 7, f"Paid: {paid_at.strftime('%Y-%m-%d')}", ln=True)
    pdf.ln(10)

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(130, 130, 130)
    pdf.cell(0, 5, f"Invoice reference: {invoice_id}", ln=True)

    return bytes(pdf.output())
