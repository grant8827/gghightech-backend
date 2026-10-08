"""add reviews table

Revision ID: a41f7e02c9d5
Revises: 7c2a9d4e1b63
Create Date: 2026-10-08 10:00:00.000000

Public client reviews — see app/models/review.py.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a41f7e02c9d5'
down_revision: Union[str, None] = '7c2a9d4e1b63'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('reviews',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('name', sa.String(length=80), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('is_featured', sa.Boolean(), server_default='false', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('moderated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('moderated_by', sa.String(length=255), nullable=True),
    sa.CheckConstraint("status IN ('PENDING', 'PUBLISHED', 'HIDDEN')", name='ck_reviews_status_valid'),
    sa.CheckConstraint('rating BETWEEN 1 AND 5', name='ck_reviews_rating_range'),
    sa.CheckConstraint("NOT is_featured OR status = 'PUBLISHED'", name='ck_reviews_featured_is_published'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_reviews_status_created_at', 'reviews', ['status', 'created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_reviews_status_created_at', table_name='reviews')
    op.drop_table('reviews')
