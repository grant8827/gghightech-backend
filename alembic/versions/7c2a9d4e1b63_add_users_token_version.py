"""add users.token_version

Revision ID: 7c2a9d4e1b63
Revises: ebfb573f672a
Create Date: 2026-10-07 10:00:00.000000

Session revocation (SEC-06): every access token carries the token_version
its user had when it was issued, and a token whose value no longer matches
is rejected — see app/services/auth.py. Existing rows get 0, which is also
what tokens issued before this column existed are treated as, so applying
this migration signs nobody out.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c2a9d4e1b63'
down_revision: Union[str, None] = 'ebfb573f672a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('users', 'token_version')
