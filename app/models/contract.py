"""Project contracts — drafted by staff in the admin Invoicing / Contracts
tab, sent to the client, and e-signed by the client's CLIENT_ADMIN in the
portal (app/api/routes/contracts.py).

Lifecycle: DRAFT -> SENT -> SIGNED, or VOID at any point. Editing a SENT
contract returns it to DRAFT (it must be sent again before it can be
signed), and a SIGNED contract can't be edited at all. At signing, a
SHA-256 of the contract's content is stored with the signer's name, time
and IP, so the signed version can be shown to be unchanged later.

Row-level security matches the other tenant tables (projects, invoices…).
"""

import uuid
from datetime import date, datetime
from typing import Optional

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Numeric, String, Text, event, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base
from app.models.numbering import next_document_number

CONTRACT_STATUSES = ("DRAFT", "SENT", "SIGNED", "VOID")


class Contract(Base):
    __tablename__ = "contracts"
    __table_args__ = (CheckConstraint(f"status IN {CONTRACT_STATUSES}", name="ck_contracts_status_valid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"))
    contract_number: Mapped[Optional[str]] = mapped_column(String(20), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")

    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    client_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    effective_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    scope: Mapped[str] = mapped_column(Text, nullable=False)
    deliverables: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)  # [str]
    timeline: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # [{label, amount, due}] — due is free text ("On signing", "2026-12-01").
    payment_schedule: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    total_value: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    payment_terms: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    terms: Mapped[str] = mapped_column(Text, nullable=False)

    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # The staff member who sent it — printed as signing for GG HighTech.
    provider_signer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    signed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    signer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    signer_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    signer_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    signed_content_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(Contract, "before_insert")
def _assign_number(mapper, connection, target: Contract) -> None:
    if not target.contract_number:
        target.contract_number = next_document_number(
            connection, Contract.__table__, Contract.__table__.c.contract_number, "contract_number_seq", "CTR"
        )
