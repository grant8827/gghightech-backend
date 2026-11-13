"""add projects.commit_activity

Revision ID: 6e3a1b8c4f52
Revises: 2b7d9e4f6a18
Create Date: 2026-11-13 16:00:00.000000

Weekly commit counts for the client dashboard's activity chart — see
app/models/project.py and app/services/github_service.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '6e3a1b8c4f52'
down_revision: Union[str, None] = '2b7d9e4f6a18'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('commit_activity', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'commit_activity')
