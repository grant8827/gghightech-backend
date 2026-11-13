"""Client-facing PDF documents: invoices and contracts.

Both share one letterhead (logo, business details from BUSINESS_* settings,
a gold rule) so everything a client receives looks like it came from the
same company. Receipts stay in app/services/pdf.py.

fpdf2's built-in Helvetica only covers latin-1, so every piece of free text
goes through _safe() (typographic quotes and dashes become ASCII; anything
else unsupported becomes "?") rather than failing the whole document.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

from fpdf import FPDF
from fpdf.enums import XPos, YPos
from fpdf.fonts import FontFace

from app.core.config import settings

INK = (17, 24, 39)
MUTED = (107, 114, 128)
HAIRLINE = (229, 231, 235)
ZEBRA = (249, 250, 251)
GOLD = (232, 184, 75)
GOLD_DARK = (138, 90, 23)
PAID_GREEN = (5, 150, 105)
LOGO = Path(__file__).resolve().parent.parent / "assets" / "logo.jpg"

_TYPOGRAPHY = str.maketrans(
    {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', "…": "...", "•": "-"}
)


def _safe(text: Optional[str]) -> str:
    return (text or "").translate(_TYPOGRAPHY).encode("latin-1", errors="replace").decode("latin-1")


def _money(n: float) -> str:
    return f"${n:,.2f}"


def _date(d: Optional[Union[date, datetime]]) -> str:
    return d.strftime("%B %d, %Y") if d else "-"


class _Doc(FPDF):
    def __init__(self, footer_note: str):
        super().__init__(format="Letter", unit="mm")
        self.footer_note = footer_note
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(auto=True, margin=22)
        self.alias_nb_pages()

    def footer(self) -> None:
        self.set_y(-14)
        self.set_draw_color(*HAIRLINE)
        self.line(18, self.get_y() - 2, self.w - 18, self.get_y() - 2)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 5, _safe(self.footer_note), align="L")
        self.set_x(18)
        self.cell(0, 5, f"Page {self.page_no()} of {{nb}}", align="R")

    # ---- building blocks ----
    def letterhead(self, doc_title: str, meta: list[tuple[str, str]], status: Optional[tuple[str, tuple]] = None) -> None:
        top = self.get_y()
        x = 18
        if LOGO.exists():
            self.image(str(LOGO), x=x, y=top, h=15)
            x += 19
        self.set_xy(x, top + 1)
        self.set_font("Helvetica", "B", 15)
        self.set_text_color(*INK)
        self.cell(80, 7, _safe(settings.BUSINESS_NAME), new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 8.5)
        self.set_text_color(*MUTED)
        lines = [ln for ln in settings.BUSINESS_ADDRESS.replace("\\n", "\n").split("\n") if ln.strip()]
        lines += [v for v in (settings.BUSINESS_EMAIL, settings.BUSINESS_PHONE, settings.BUSINESS_WEBSITE) if v]
        for ln in lines:
            self.set_x(x)
            self.cell(80, 4.2, _safe(ln), new_x=XPos.LEFT, new_y=YPos.NEXT)
        left_bottom = self.get_y()

        # Right column: title + meta table.
        right_w = 72
        rx = self.w - 18 - right_w
        self.set_xy(rx, top)
        self.set_font("Helvetica", "B", 22)
        self.set_text_color(*INK)
        self.cell(right_w, 10, doc_title, align="R", new_x=XPos.LEFT, new_y=YPos.NEXT)
        self.set_font("Helvetica", "", 9)
        for label, value in meta:
            self.set_x(rx)
            self.set_text_color(*MUTED)
            self.cell(right_w - 40, 5, label, align="R")
            self.set_text_color(*INK)
            self.set_font("Helvetica", "B", 9)
            self.cell(40, 5, _safe(value), align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            self.set_font("Helvetica", "", 9)
        if status:
            text, color = status
            self.set_x(rx)
            self.set_font("Helvetica", "B", 8)
            w = self.get_string_width(text) + 8
            self.set_xy(self.w - 18 - w, self.get_y() + 1.5)
            self.set_fill_color(*color)
            self.set_text_color(255, 255, 255)
            self.cell(w, 5.5, text, align="C", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)

        self.set_y(max(left_bottom, self.get_y()) + 5)
        self.set_draw_color(*GOLD)
        self.set_line_width(0.8)
        self.line(18, self.get_y(), self.w - 18, self.get_y())
        self.set_line_width(0.2)
        self.ln(7)

    def label(self, text: str) -> None:
        self.set_font("Helvetica", "B", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4.5, text.upper(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    def two_blocks(self, left: tuple[str, list[str]], right: tuple[str, list[str]]) -> None:
        top = self.get_y()
        col = (self.w - 36) / 2
        bottoms = []
        for i, (title, lines) in enumerate((left, right)):
            x = 18 + i * (col + 6)
            self.set_xy(x, top)
            self.set_font("Helvetica", "B", 7.5)
            self.set_text_color(*MUTED)
            self.cell(col, 4.5, title.upper(), new_x=XPos.LEFT, new_y=YPos.NEXT)
            for j, ln in enumerate(lines):
                self.set_x(x)
                self.set_font("Helvetica", "B" if j == 0 else "", 10 if j == 0 else 9)
                self.set_text_color(*INK if j == 0 else MUTED)
                self.multi_cell(col, 5, _safe(ln), new_x=XPos.LEFT, new_y=YPos.NEXT)
            bottoms.append(self.get_y())
        self.set_y(max(bottoms) + 6)

    def section(self, number: int, title: str) -> None:
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(2)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*INK)
        self.cell(0, 6, f"{number}.  {title}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*HAIRLINE)
        self.line(18, self.get_y() + 0.5, self.w - 18, self.get_y() + 0.5)
        self.ln(3)

    def paragraph(self, text: str, size: float = 9.5) -> None:
        self.set_font("Helvetica", "", size)
        self.set_text_color(55, 65, 81)
        for para in _safe(text).replace("\r\n", "\n").split("\n\n"):
            if para.strip():
                self.multi_cell(0, 5, para.strip(), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                self.ln(1.5)

    def watermark(self, text: str, color: tuple) -> None:
        self.set_font("Helvetica", "B", 70)
        self.set_text_color(*color)
        with self.local_context(fill_opacity=0.08, stroke_opacity=0.08):
            with self.rotation(35, self.w / 2, self.h / 2):
                self.set_xy(0, self.h / 2 - 15)
                self.cell(self.w, 30, text, align="C")


# ---------------------------------------------------------------- invoice


@dataclass
class InvoiceDoc:
    invoice_id: uuid.UUID
    number: str
    project_title: str
    client_org: str
    bill_to_name: Optional[str]
    bill_to_email: Optional[str]
    line_items: list[dict]
    subtotal: float
    tax_rate: float
    tax_amount: float
    total: float
    status: str
    issue_date: date
    due_date: Optional[date]
    paid_at: Optional[datetime] = None
    notes: Optional[str] = None
    pay_url: Optional[str] = None


def build_invoice_document(doc: InvoiceDoc) -> bytes:
    pdf = _Doc(f"{settings.BUSINESS_NAME} - Invoice {doc.number}")
    pdf.add_page()
    paid = doc.status == "PAID"
    overdue = not paid and doc.due_date is not None and doc.due_date < date.today()
    status = ("PAID", PAID_GREEN) if paid else ("OVERDUE", (220, 38, 38)) if overdue else ("DUE", GOLD_DARK)
    pdf.letterhead(
        "INVOICE",
        [
            ("Invoice no.", doc.number),
            ("Issue date", _date(doc.issue_date)),
            ("Due date", _date(doc.due_date) if doc.due_date else "On receipt"),
        ],
        status=status,
    )

    bill_lines = [doc.bill_to_name or doc.client_org]
    if doc.bill_to_name and doc.client_org and doc.client_org != doc.bill_to_name:
        bill_lines.append(doc.client_org)
    if doc.bill_to_email:
        bill_lines.append(doc.bill_to_email)
    pdf.two_blocks(("Bill to", bill_lines), ("Project", [doc.project_title, f"Amount due: {_money(0 if paid else doc.total)}"]))

    # Line items.
    pdf.set_font("Helvetica", "", 9.5)
    pdf.set_text_color(*INK)
    pdf.set_draw_color(*HAIRLINE)
    pdf.set_fill_color(255, 255, 255)  # alternate rows use the current fill
    with pdf.table(
        col_widths=(96, 18, 32, 34),
        text_align=("LEFT", "RIGHT", "RIGHT", "RIGHT"),
        headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=INK, size_pt=8.5),
        borders_layout="HORIZONTAL_LINES",
        cell_fill_color=ZEBRA,
        cell_fill_mode="ROWS",
        line_height=6.5,
        padding=(2.2, 2.5),
    ) as table:
        head = table.row()
        for h in ("DESCRIPTION", "QTY", "RATE", "AMOUNT"):
            head.cell(h)
        for item in doc.line_items:
            qty = float(item["quantity"])
            price = float(item["unit_price"])
            row = table.row()
            row.cell(_safe(item["description"]))
            row.cell(f"{qty:g}")
            row.cell(_money(price))
            row.cell(_money(round(qty * price, 2)))

    # Totals.
    pdf.ln(4)
    box_w, label_w = 80, 46
    x = pdf.w - 18 - box_w

    def total_row(label: str, value: str, bold: bool = False, fill: Optional[tuple] = None, h: float = 6.5) -> None:
        pdf.set_x(x)
        pdf.set_font("Helvetica", "B" if bold else "", 10.5 if bold else 9.5)
        if fill:
            pdf.set_fill_color(*fill)
        pdf.set_text_color(*INK if bold or fill else MUTED)
        pdf.cell(label_w, h, label, align="R", fill=bool(fill))
        pdf.set_text_color(*INK)
        pdf.cell(box_w - label_w, h, value, align="R", fill=bool(fill), new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    total_row("Subtotal", _money(doc.subtotal))
    if doc.tax_rate:
        total_row(f"Tax ({doc.tax_rate:g}%)", _money(doc.tax_amount))
    total_row("Total", _money(doc.total), bold=True)
    if paid:
        total_row(f"Paid {doc.paid_at.strftime('%b %d, %Y') if doc.paid_at else ''}", f"-{_money(doc.total)}")
    pdf.ln(1)
    total_row("Amount due  ", _money(0 if paid else doc.total), bold=True, fill=GOLD, h=9)

    pdf.ln(8)
    if doc.notes:
        pdf.label("Notes")
        pdf.paragraph(doc.notes, size=9)
        pdf.ln(2)

    pdf.label("How to pay")
    lines = []
    if paid:
        lines.append("This invoice has been paid in full. Thank you!")
    else:
        if doc.pay_url:
            lines.append(f"Pay securely online by card: {doc.pay_url}")
        lines.append("Or sign in to your client dashboard and open Payments to pay and download receipts.")
    if settings.BUSINESS_EMAIL:
        lines.append(f"Questions about this invoice? Email {settings.BUSINESS_EMAIL}.")
    pdf.paragraph("\n\n".join(lines), size=9)

    if paid:
        pdf.watermark("PAID", PAID_GREEN)
    return bytes(pdf.output())


# --------------------------------------------------------------- contract


@dataclass
class ContractDoc:
    contract_id: uuid.UUID
    number: str
    title: str
    status: str
    project_title: str
    client_org: str
    client_name: str
    client_email: Optional[str]
    effective_date: Optional[date]
    scope: str
    deliverables: list[str]
    timeline: Optional[str]
    payment_schedule: list[dict]
    total_value: float
    payment_terms: Optional[str]
    terms: str
    provider_signer_name: Optional[str] = None
    sent_at: Optional[datetime] = None
    signer_name: Optional[str] = None
    signer_email: Optional[str] = None
    signed_at: Optional[datetime] = None
    signer_ip: Optional[str] = None
    signed_content_hash: Optional[str] = None
    extra: dict = field(default_factory=dict)


def build_contract_document(doc: ContractDoc) -> bytes:
    pdf = _Doc(f"{settings.BUSINESS_NAME} - Agreement {doc.number}")
    pdf.add_page()
    status_colors = {
        "DRAFT": ("DRAFT", MUTED),
        "SENT": ("AWAITING SIGNATURE", GOLD_DARK),
        "SIGNED": ("SIGNED", PAID_GREEN),
        "VOID": ("VOID", (220, 38, 38)),
    }
    pdf.letterhead(
        "AGREEMENT",
        [
            ("Contract no.", doc.number),
            ("Effective date", _date(doc.effective_date) if doc.effective_date else "On signing"),
            ("Contract value", _money(doc.total_value)),
        ],
        status=status_colors.get(doc.status),
    )

    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*INK)
    pdf.multi_cell(0, 7, _safe(doc.title), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    provider = [settings.BUSINESS_NAME] + [v for v in (settings.BUSINESS_EMAIL, settings.BUSINESS_WEBSITE) if v]
    client = [doc.client_org or doc.client_name]
    if doc.client_name and doc.client_name != doc.client_org:
        client.append(f"Attn: {doc.client_name}")
    if doc.client_email:
        client.append(doc.client_email)
    pdf.two_blocks(("Service provider", provider), ("Client", client))
    pdf.paragraph(
        f'This Service Agreement (the "Agreement") is entered into between {settings.BUSINESS_NAME} '
        f'("Provider") and {doc.client_org or doc.client_name} ("Client") for the project '
        f'"{doc.project_title}".',
        size=9.5,
    )

    n = 1
    pdf.section(n, "Scope of work")
    pdf.paragraph(doc.scope)

    if doc.deliverables:
        n += 1
        pdf.section(n, "Deliverables")
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(55, 65, 81)
        for i, d in enumerate(doc.deliverables, 1):
            pdf.set_x(22)
            pdf.multi_cell(0, 5.2, f"{i}.  {_safe(d)}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

    if doc.timeline:
        n += 1
        pdf.section(n, "Timeline")
        pdf.paragraph(doc.timeline)

    n += 1
    pdf.section(n, "Fees and payment")
    if doc.payment_schedule:
        pdf.set_font("Helvetica", "", 9.5)
        pdf.set_text_color(*INK)
        pdf.set_draw_color(*HAIRLINE)
        pdf.set_fill_color(255, 255, 255)  # alternate rows use the current fill
        with pdf.table(
            col_widths=(96, 50, 34),
            text_align=("LEFT", "LEFT", "RIGHT"),
            headings_style=FontFace(emphasis="BOLD", color=(255, 255, 255), fill_color=INK, size_pt=8.5),
            borders_layout="HORIZONTAL_LINES",
            cell_fill_color=ZEBRA,
            cell_fill_mode="ROWS",
            line_height=6.5,
            padding=(2.2, 2.5),
        ) as table:
            head = table.row()
            for h in ("PAYMENT", "DUE", "AMOUNT"):
                head.cell(h)
            for p in doc.payment_schedule:
                row = table.row()
                row.cell(_safe(p.get("label", "")))
                row.cell(_safe(p.get("due", "")))
                row.cell(_money(float(p.get("amount", 0))))
        pdf.ln(2)
    pdf.set_font("Helvetica", "B", 10.5)
    pdf.set_text_color(*INK)
    pdf.cell(0, 7, f"Total contract value: {_money(doc.total_value)}", align="R", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if doc.payment_terms:
        pdf.ln(1)
        pdf.paragraph(doc.payment_terms)

    n += 1
    pdf.section(n, "Terms and conditions")
    pdf.paragraph(doc.terms, size=9)

    # Signatures.
    if pdf.get_y() > pdf.h - 75:
        pdf.add_page()
    n += 1
    pdf.section(n, "Signatures")
    pdf.paragraph("By signing below, both parties agree to the terms of this Agreement.", size=9)
    top = pdf.get_y() + 2
    col = (pdf.w - 36 - 10) / 2
    blocks = [
        (f"For {settings.BUSINESS_NAME}", doc.provider_signer_name, doc.sent_at, None),
        ("For the Client", doc.signer_name, doc.signed_at, doc.signer_email),
    ]
    for i, (title, name, when, email) in enumerate(blocks):
        x = 18 + i * (col + 10)
        pdf.set_xy(x, top)
        pdf.set_font("Helvetica", "B", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(col, 4.5, title.upper(), new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_x(x)
        signed = bool(name and when and (i == 0 or doc.status == "SIGNED"))
        pdf.set_font("Helvetica", "I", 16)
        pdf.set_text_color(*INK)
        pdf.cell(col, 12, _safe(name) if signed else "", new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_draw_color(*INK)
        pdf.line(x, pdf.get_y(), x + col, pdf.get_y())
        pdf.ln(1.5)
        pdf.set_x(x)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*MUTED)
        pdf.cell(col, 4.5, f"Name: {_safe(name) if signed else ''}", new_x=XPos.LEFT, new_y=YPos.NEXT)
        pdf.set_x(x)
        pdf.cell(col, 4.5, f"Date: {when.strftime('%B %d, %Y') if signed and when else ''}", new_x=XPos.LEFT, new_y=YPos.NEXT)
        if signed and email:
            pdf.set_x(x)
            pdf.cell(col, 4.5, f"Email: {_safe(email)}", new_x=XPos.LEFT, new_y=YPos.NEXT)

    if doc.status == "SIGNED" and doc.signed_at:
        pdf.set_y(pdf.get_y() + 6)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(
            0,
            4,
            _safe(
                f"Electronically signed by {doc.signer_name} on "
                f"{doc.signed_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                f"{f' from IP {doc.signer_ip}' if doc.signer_ip else ''} through the {settings.BUSINESS_NAME} client portal. "
                f"Document fingerprint (SHA-256): {doc.signed_content_hash or '-'}"
            ),
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    if doc.status == "DRAFT":
        pdf.watermark("DRAFT", MUTED)
    elif doc.status == "VOID":
        pdf.watermark("VOID", (220, 38, 38))
    return bytes(pdf.output())
