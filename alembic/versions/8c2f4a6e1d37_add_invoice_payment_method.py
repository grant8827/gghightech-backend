"""add invoice payment_method and payment_reference

Revision ID: 8c2f4a6e1d37
Revises: 5a1c7e3d9b20
Create Date: 2026-11-13 12:00:00.000000

Records how a PAID invoice was paid (card via Stripe, or a check / wire /
cash payment staff recorded), so the client portal can show and print a
receipt for it — see app/models/invoice.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c2f4a6e1d37'
down_revision: Union[str, None] = '5a1c7e3d9b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('invoices', sa.Column('payment_method', sa.String(length=20), nullable=True))
    op.add_column('invoices', sa.Column('payment_reference', sa.String(length=120), nullable=True))
    op.create_check_constraint(
        'ck_invoices_payment_method_valid',
        'invoices',
        "payment_method IS NULL OR payment_method IN ('CARD', 'CHECK', 'WIRE', 'CASH', 'OTHER')",
    )
    # Invoices already marked paid before this column existed: the method
    # wasn't recorded, so file them under OTHER rather than leave a PAID
    # invoice without one.
    op.execute("UPDATE invoices SET payment_method = 'OTHER' WHERE status = 'PAID' AND payment_method IS NULL")


def downgrade() -> None:
    op.drop_constraint('ck_invoices_payment_method_valid', 'invoices', type_='check')
    op.drop_column('invoices', 'payment_reference')
    op.drop_column('invoices', 'payment_method')
