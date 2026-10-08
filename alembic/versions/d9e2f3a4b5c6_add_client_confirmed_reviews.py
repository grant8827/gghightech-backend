"""add four client-confirmed reviews

Revision ID: d9e2f3a4b5c6
Revises: c5d8e1f0a7b2
Create Date: 2026-10-08 17:00:00.000000

Four reviews whose wording was drafted with GG HighTech and then confirmed
by each client (October 2026). Same approach as c5d8e1f0a7b2: a migration
so they exist in every environment, fixed ids with ON CONFLICT DO NOTHING
so it is safe to re-run and never resurrects a review staff have since
deleted, and the shared LEGACY_EMAIL marker because no reviewer email
address was collected.

S. Gordon gave 4.5; the site stores whole stars, recorded as 4 at the
owner's instruction.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9e2f3a4b5c6'
down_revision: Union[str, None] = 'c5d8e1f0a7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_EMAIL = "legacy-import@gghightech.internal"

# Dated the month the clients confirmed them. Times are a minute apart only
# to give the list a stable order.
REVIEWS = [
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e03",
        "name": "Mr. Forbs",
        "rating": 4,
        "created_at": "2026-10-08T12:00:00+00:00",
        "message": (
            "GG-HighTech developed a mobile application for our radio station, helping us bring our broadcasts "
            "closer to our listeners. The team understood our requirements and delivered a solution tailored "
            "to our needs."
        ),
    },
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e04",
        "name": "T. Grant",
        "rating": 5,
        "created_at": "2026-10-08T12:01:00+00:00",
        "message": (
            "GG-HighTech demonstrated strong technical expertise in developing a radio broadcasting platform. "
            "Their attention to functionality, performance, and user experience made the development process "
            "a positive experience."
        ),
    },
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e05",
        "name": "S. Gordon",
        "rating": 4,
        "created_at": "2026-10-08T12:02:00+00:00",
        "message": (
            "GG-HighTech developed a business management solution designed around our operational needs. "
            "Their understanding of complex business processes and ability to translate requirements into "
            "practical software solutions was valuable."
        ),
    },
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e06",
        "name": "Pastor Grant",
        "rating": 5,
        "created_at": "2026-10-08T12:03:00+00:00",
        "message": (
            "GG-HighTech demonstrated professionalism and dedication in developing a platform to support "
            "church financial management. Their attention to our requirements and commitment to building a "
            "practical, easy-to-use solution was appreciated."
        ),
    },
]


def upgrade() -> None:
    insert = sa.text(
        """
        INSERT INTO reviews (id, name, email, rating, message, status, is_featured, created_at)
        VALUES (CAST(:id AS uuid), :name, :email, :rating, :message, 'PUBLISHED', false,
                CAST(:created_at AS timestamptz))
        ON CONFLICT (id) DO NOTHING
        """
    )
    bind = op.get_bind()
    for review in REVIEWS:
        bind.execute(insert, {**review, "email": LEGACY_EMAIL})


def downgrade() -> None:
    bind = op.get_bind()
    for review in REVIEWS:
        bind.execute(sa.text("DELETE FROM reviews WHERE id = CAST(:id AS uuid)"), {"id": review["id"]})
