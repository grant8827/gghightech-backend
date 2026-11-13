import uuid
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal, Optional

from pydantic import BaseModel, EmailStr, Field, model_validator


class InvoiceOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    project_id: uuid.UUID
    milestone_id: Optional[uuid.UUID]
    description: Optional[str]
    amount: float
    status: str
    customer_email: Optional[str]
    created_at: datetime
    paid_at: Optional[datetime]
    payment_method: Optional[str] = None
    payment_reference: Optional[str] = None
    invoice_number: Optional[str] = None
    line_items: Optional[list["LineItem"]] = None
    subtotal: Optional[float] = None
    tax_rate: Optional[float] = None
    tax_amount: Optional[float] = None
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    notes: Optional[str] = None
    bill_to_name: Optional[str] = None
    sent_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LineItem(BaseModel):
    description: str = Field(..., min_length=1, max_length=300)
    quantity: float = Field(..., gt=0, le=100_000)
    unit_price: float = Field(..., ge=0, le=10_000_000)


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def invoice_totals(items: list[LineItem], tax_rate: float) -> tuple[float, float, float]:
    """(subtotal, tax, total), each rounded to cents, in Decimal so that
    e.g. 3 x $19.99 is exactly $59.97."""
    subtotal = sum((_money(Decimal(str(i.quantity)) * Decimal(str(i.unit_price))) for i in items), Decimal("0"))
    tax = _money(subtotal * Decimal(str(tax_rate)) / Decimal("100"))
    return float(subtotal), float(tax), float(subtotal + tax)


class InvoiceCreate(BaseModel):
    """Staff-only direct creation (POST /invoices) — not tied to a
    milestone. Either itemized (line_items, optional tax_rate) or, for
    older callers, a single amount with a description. The total is always
    computed here, never taken from the client."""

    project_id: uuid.UUID
    line_items: Optional[list[LineItem]] = Field(None, min_length=1, max_length=50)
    tax_rate: float = Field(0, ge=0, le=100)
    amount: Optional[float] = Field(None, gt=0)
    # A short title for the invoice (emails, lists, Stripe). Defaults to the
    # first line item.
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    customer_email: Optional[EmailStr] = None
    bill_to_name: Optional[str] = Field(None, max_length=255)
    issue_date: Optional[date] = None
    due_date: Optional[date] = None
    notes: Optional[str] = Field(None, max_length=2000)

    @model_validator(mode="after")
    def _items_or_amount(self) -> "InvoiceCreate":
        if not self.line_items:
            if self.amount is None or not self.description:
                raise ValueError("Add at least one line item")
            self.line_items = [LineItem(description=self.description, quantity=1, unit_price=self.amount)]
        if self.issue_date and self.due_date and self.due_date < self.issue_date:
            raise ValueError("The due date can't be before the issue date")
        if invoice_totals(self.line_items, self.tax_rate)[2] <= 0:
            raise ValueError("The invoice total must be more than $0")
        if not self.description:
            first = self.line_items[0].description
            more = len(self.line_items) - 1
            self.description = (f"{first} + {more} more" if more else first)[:500]
        return self


class InvoiceSent(BaseModel):
    invoice: InvoiceOut
    email_sent: bool
    checkout_url: Optional[str] = None


class InvoiceMarkPaid(BaseModel):
    """Optional body for PATCH /invoices/{id}/mark-paid — how the client
    paid outside Stripe. Shown on the client's receipt. CARD isn't offered:
    only the Stripe webhook records card payments."""

    payment_method: Literal["CHECK", "WIRE", "CASH", "OTHER"] = "OTHER"
    payment_reference: Optional[str] = Field(None, max_length=120)


InvoiceOut.model_rebuild()
