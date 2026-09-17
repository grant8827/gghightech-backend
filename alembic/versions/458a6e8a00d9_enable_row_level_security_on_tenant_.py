"""enable row level security on tenant tables

Revision ID: 458a6e8a00d9
Revises: 9e9404e6376e
Create Date: 2026-09-16 13:55:11.084618

Defense-in-depth underneath the app-level org_id filtering that already
exists (app/services/auth.py's resolve_org_id) — a bug or a future raw
query still can't cross tenants. The two session GUC vars this reads
(app.bypass_rls, app.current_org_id) are set per-request via SET LOCAL in
resolve_org_id / require_roles, so they never leak across pooled
connections.

FORCE ROW LEVEL SECURITY matters here: without it, Postgres exempts the
table owner from RLS entirely — and in this local/dev setup the app's own
DB role *is* the table owner (it ran the migrations that created these
tables), so the policy would silently do nothing without this.
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '458a6e8a00d9'
down_revision: Union[str, None] = '9e9404e6376e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("projects", "milestones", "invoices", "project_updates")


def upgrade() -> None:
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (
                current_setting('app.bypass_rls', true) = 'true'
                OR org_id = current_setting('app.current_org_id', true)::uuid
            )
            """
        )


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
