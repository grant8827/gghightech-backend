"""add instant_payments table

Revision ID: f3b6c1d8e924
Revises: d9e2f3a4b5c6
Create Date: 2026-11-02 12:00:00.000000

Staff-sent Stripe payment links by email + amount — see
app/models/instant_payment.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3b6c1d8e924'
down_revision: Union[str, None] = 'd9e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('instant_payments',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('customer_email', sa.String(length=255), nullable=False),
    sa.Column('amount', sa.Numeric(precision=10, scale=2), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('checkout_url', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('PENDING', 'PAID')", name='ck_instant_payments_status_valid'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('instant_payments')
