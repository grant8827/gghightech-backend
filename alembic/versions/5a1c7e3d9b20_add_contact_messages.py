"""add contact_messages

Revision ID: 5a1c7e3d9b20
Revises: b7e4c2a9f051
Create Date: 2026-11-11 12:00:00.000000

Messages from the public contact form and floating message button, read
by staff in the admin Messages tab — see app/models/contact_message.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '5a1c7e3d9b20'
down_revision: Union[str, None] = 'b7e4c2a9f051'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'contact_messages',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('phone', sa.String(length=40), nullable=True),
        sa.Column('subject', sa.String(length=150), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=20), server_default='NEW', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True),
        sa.Column('handled_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('handled_by', sa.String(length=255), nullable=True),
        sa.CheckConstraint("status IN ('NEW', 'READ', 'ARCHIVED')", name='ck_contact_messages_status_valid'),
        sa.CheckConstraint("source IN ('CONTACT_PAGE', 'WIDGET')", name='ck_contact_messages_source_valid'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_contact_messages_status_created_at', 'contact_messages', ['status', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_contact_messages_status_created_at', table_name='contact_messages')
    op.drop_table('contact_messages')
