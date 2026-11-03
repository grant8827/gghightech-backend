"""add subscription plan length and Stripe subscription tracking

Revision ID: b7e4c2a9f051
Revises: f3b6c1d8e924
Create Date: 2026-11-03 12:00:00.000000

Fixed-length plans (total_cycles / cycles_billed), a COMPLETED status,
and the Stripe subscription id — see app/models/subscription_plan.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7e4c2a9f051'
down_revision: Union[str, None] = 'f3b6c1d8e924'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('subscription_plans', sa.Column('total_cycles', sa.Integer(), nullable=True))
    op.add_column('subscription_plans', sa.Column('cycles_billed', sa.Integer(), server_default='0', nullable=False))
    op.add_column('subscription_plans', sa.Column('stripe_subscription_id', sa.String(length=255), nullable=True))
    op.add_column('subscription_plans', sa.Column('last_stripe_invoice_id', sa.String(length=255), nullable=True))
    op.create_check_constraint(
        'ck_subscription_plans_total_cycles_range', 'subscription_plans', 'total_cycles BETWEEN 1 AND 120'
    )
    op.drop_constraint('ck_subscription_plans_status_valid', 'subscription_plans', type_='check')
    op.create_check_constraint(
        'ck_subscription_plans_status_valid',
        'subscription_plans',
        "status IN ('ACTIVE', 'PAUSED', 'CANCELED', 'COMPLETED')",
    )


def downgrade() -> None:
    op.execute("UPDATE subscription_plans SET status = 'CANCELED' WHERE status = 'COMPLETED'")
    op.drop_constraint('ck_subscription_plans_status_valid', 'subscription_plans', type_='check')
    op.create_check_constraint(
        'ck_subscription_plans_status_valid', 'subscription_plans', "status IN ('ACTIVE', 'PAUSED', 'CANCELED')"
    )
    op.drop_constraint('ck_subscription_plans_total_cycles_range', 'subscription_plans', type_='check')
    op.drop_column('subscription_plans', 'last_stripe_invoice_id')
    op.drop_column('subscription_plans', 'stripe_subscription_id')
    op.drop_column('subscription_plans', 'cycles_billed')
    op.drop_column('subscription_plans', 'total_cycles')
