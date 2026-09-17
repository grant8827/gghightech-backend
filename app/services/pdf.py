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


def build_estimate_pdf(
    estimate_id: uuid.UUID,
    project_type: str,
    features: list[str],
    design_tier: str,
    price_min: float,
    price_max: float,
    weeks_min: int,
    weeks_max: int,
    project_description: Optional[str] = None,
    client_email: Optional[str] = None,
    client_phone: Optional[str] = None,
) -> bytes:
    pdf = FPDF(format="Letter")
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(*BRAND_COLOR)
    pdf.cell(0, 12, "GG HighTech", ln=True)

    pdf.set_font("Helvetica", "", 12)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 8, "Technical Scope Proposal", ln=True)
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
    pdf.cell(0, 7, f"Design tier: {design_tier.replace('_', ' ').title()}", ln=True)
    features_label = ", ".join(f.replace("_", " ").title() for f in features) or "None"
    pdf.cell(0, 7, f"Features: {features_label}", ln=True)
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Estimate", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Budget range: ${price_min:,.0f} - ${price_max:,.0f}", ln=True)
    pdf.cell(0, 7, f"Timeline: {weeks_min} - {weeks_max} weeks", ln=True)
    pdf.ln(10)

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
