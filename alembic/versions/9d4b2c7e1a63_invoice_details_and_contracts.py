"""professional invoice fields and contracts

Revision ID: 9d4b2c7e1a63
Revises: 6e3a1b8c4f52
Create Date: 2026-11-13 18:00:00.000000

- invoices: invoice_number (INV-YYYY-NNNN from invoice_number_seq), line
  items, subtotal / tax, issue & due dates, notes, bill-to name, sent_at.
  Existing invoices are numbered in creation order.
- contracts: new tenant table with the same row-level security policy as
  projects / invoices (see 458a6e8a00d9 and e90e0a659dc5).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '9d4b2c7e1a63'
down_revision: Union[str, None] = '6e3a1b8c4f52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TENANT_POLICY = (
    "current_setting('app.bypass_rls', true) = 'true' "
    "OR org_id = NULLIF(current_setting('app.current_org_id', true), '')::uuid"
)


def upgrade() -> None:
    # invoices is under FORCE ROW LEVEL SECURITY: without the bypass, the
    # backfill below would only "see" rows if the migration role happened
    # to be a superuser.
    op.execute("SET LOCAL app.bypass_rls = 'true'")

    op.add_column('invoices', sa.Column('invoice_number', sa.String(length=20), nullable=True))
    op.add_column('invoices', sa.Column('line_items', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column('invoices', sa.Column('subtotal', sa.Numeric(10, 2), nullable=True))
    op.add_column('invoices', sa.Column('tax_rate', sa.Numeric(5, 2), nullable=True))
    op.add_column('invoices', sa.Column('tax_amount', sa.Numeric(10, 2), nullable=True))
    op.add_column('invoices', sa.Column('issue_date', sa.Date(), nullable=True))
    op.add_column('invoices', sa.Column('due_date', sa.Date(), nullable=True))
    op.add_column('invoices', sa.Column('notes', sa.Text(), nullable=True))
    op.add_column('invoices', sa.Column('bill_to_name', sa.String(length=255), nullable=True))
    op.add_column('invoices', sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True))

    op.execute("CREATE SEQUENCE invoice_number_seq")
    op.execute(
        """
        UPDATE invoices i SET invoice_number = n.num
        FROM (
            SELECT id, 'INV-' || to_char(created_at, 'YYYY') || '-' ||
                   lpad((row_number() OVER (ORDER BY created_at, id))::text, 4, '0') AS num
            FROM invoices
        ) n
        WHERE i.id = n.id
        """
    )
    op.execute("SELECT setval('invoice_number_seq', GREATEST((SELECT count(*) FROM invoices), 1), (SELECT count(*) FROM invoices) > 0)")
    op.execute("UPDATE invoices SET issue_date = created_at::date WHERE issue_date IS NULL")
    # 8c2f4a6e1d37 tried this without the RLS bypass; repeat it here.
    op.execute("UPDATE invoices SET payment_method = 'OTHER' WHERE status = 'PAID' AND payment_method IS NULL")
    op.create_unique_constraint('uq_invoices_invoice_number', 'invoices', ['invoice_number'])

    op.execute("CREATE SEQUENCE contract_number_seq")
    op.create_table(
        'contracts',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('project_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('contract_number', sa.String(length=20), nullable=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='DRAFT', nullable=False),
        sa.Column('client_name', sa.String(length=255), nullable=False),
        sa.Column('client_email', sa.String(length=255), nullable=True),
        sa.Column('effective_date', sa.Date(), nullable=True),
        sa.Column('scope', sa.Text(), nullable=False),
        sa.Column('deliverables', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('timeline', sa.Text(), nullable=True),
        sa.Column('payment_schedule', postgresql.JSONB(astext_type=sa.Text()), server_default='[]', nullable=False),
        sa.Column('total_value', sa.Numeric(12, 2), server_default='0', nullable=False),
        sa.Column('payment_terms', sa.Text(), nullable=True),
        sa.Column('terms', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('provider_signer_name', sa.String(length=255), nullable=True),
        sa.Column('signed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('signer_name', sa.String(length=255), nullable=True),
        sa.Column('signer_email', sa.String(length=255), nullable=True),
        sa.Column('signer_ip', sa.String(length=64), nullable=True),
        sa.Column('signed_content_hash', sa.String(length=64), nullable=True),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('DRAFT', 'SENT', 'SIGNED', 'VOID')", name='ck_contracts_status_valid'),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('contract_number', name='uq_contracts_contract_number'),
    )
    op.create_index('ix_contracts_org_id', 'contracts', ['org_id'])
    op.create_index('ix_contracts_project_id', 'contracts', ['project_id'])
    op.execute("ALTER TABLE contracts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE contracts FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tenant_isolation ON contracts USING ({TENANT_POLICY})")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON contracts")
    op.drop_index('ix_contracts_project_id', table_name='contracts')
    op.drop_index('ix_contracts_org_id', table_name='contracts')
    op.drop_table('contracts')
    op.execute("DROP SEQUENCE IF EXISTS contract_number_seq")
    op.drop_constraint('uq_invoices_invoice_number', 'invoices', type_='unique')
    for column in ('sent_at', 'bill_to_name', 'notes', 'due_date', 'issue_date', 'tax_amount', 'tax_rate',
                   'subtotal', 'line_items', 'invoice_number'):
        op.drop_column('invoices', column)
    op.execute("DROP SEQUENCE IF EXISTS invoice_number_seq")
