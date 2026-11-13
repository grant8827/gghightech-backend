"""add projects.live_url and projects.recent_commits

Revision ID: 2b7d9e4f6a18
Revises: 8c2f4a6e1d37
Create Date: 2026-11-13 15:00:00.000000

live_url is the client's production domain; recent_commits caches the last
few GitHub commits for the client dashboard — see app/models/project.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '2b7d9e4f6a18'
down_revision: Union[str, None] = '8c2f4a6e1d37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('projects', sa.Column('live_url', sa.Text(), nullable=True))
    op.add_column('projects', sa.Column('recent_commits', postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column('projects', 'recent_commits')
    op.drop_column('projects', 'live_url')
