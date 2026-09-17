"""fix RLS policy uuid cast on reset GUC placeholder

Revision ID: e90e0a659dc5
Revises: 458a6e8a00d9
Create Date: 2026-09-16 14:20:00.000000

The original policy (458a6e8a00d9) assumed `bypass_rls = 'true' OR
org_id = current_setting('app.current_org_id', true)::uuid` would
short-circuit and never evaluate the cast when bypass_rls is true. Two
things make that assumption wrong:

1. Postgres does not guarantee left-to-right short-circuit evaluation of
   OR in a WHERE/policy expression — the planner is free to evaluate
   sub-expressions in whatever order it estimates as cheapest (this is
   documented Postgres behavior, not a bug).
2. A custom GUC placeholder (app.current_org_id is not a real declared
   parameter, just an ad-hoc one) that has been SET LOCAL at least once on
   a pooled connection reverts to an empty string — not NULL — once that
   transaction ends, once the placeholder itself has been created.

Combined, a staff request (bypass_rls=true, current_org_id never touched)
reusing a pooled connection that a client request had earlier used would
hit `''::uuid`, which raises instead of evaluating to NULL. Wrapping in
NULLIF(..., '') makes the cast produce NULL instead of raising in every
case (never-set, reset-to-empty, or a real UUID), independent of
evaluation order.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e90e0a659dc5'
down_revision: Union[str, None] = '458a6e8a00d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("projects", "milestones", "invoices", "project_updates")

OLD_EXPR = """
    current_setting('app.bypass_rls', true) = 'true'
    OR org_id = current_setting('app.current_org_id', true)::uuid
"""

NEW_EXPR = """
    current_setting('app.bypass_rls', true) = 'true'
    OR org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid
"""


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({NEW_EXPR})")


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY tenant_isolation ON {table}")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({OLD_EXPR})")
