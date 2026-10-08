"""import reviews recovered from the previous website

Revision ID: c5d8e1f0a7b2
Revises: a41f7e02c9d5
Create Date: 2026-10-08 15:00:00.000000

Two genuine client reviews carried over from the old GG HighTech site,
with their original wording, rating and month. Done as a migration so they
exist in every environment, not just wherever someone ran a script.

The old site didn't record reviewer email addresses and `reviews.email` is
required, so these rows carry LEGACY_EMAIL as a marker — it is never shown
publicly, and in the admin Reviews tab it identifies a row as imported
rather than submitted through the form.

Safe to re-run (fixed ids, ON CONFLICT DO NOTHING), and it never
resurrects a review staff have since deleted or changed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5d8e1f0a7b2'
down_revision: Union[str, None] = 'a41f7e02c9d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_EMAIL = "legacy-import@gghightech.internal"

# created_at is the first of the month the review was originally given, at
# midday UTC so it reads as that month in every visitor's timezone.
LEGACY_REVIEWS = [
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e01",
        "name": "Mr. Rechard",
        "rating": 5,
        "created_at": "2022-03-01T12:00:00+00:00",
        "message": (
            "GG-HighTech developed my radio station website and mobile application, and everything was "
            "delivered on time as promised. Their professionalism, reliability, and commitment to delivering "
            "quality work made the entire experience excellent. I trusted them with my project, and they "
            "delivered. I believe you can trust them with yours too!"
        ),
    },
    {
        "id": "0b8f6a52-3c1d-4e7a-9f20-6d5c4b3a2e02",
        "name": "Ms. Berry",
        "rating": 4,
        "created_at": "2026-05-01T12:00:00+00:00",
        "message": (
            "GG-HighTech developed a comprehensive web application for streaming live events, complete with a "
            "built-in broadcasting platform and mobile application. Both applications were delivered according "
            "to our specific requirements and expectations. Their ability to understand our vision and "
            "transform it into a fully functional platform was impressive. We trusted GG-HighTech with our "
            "project, and they delivered. I would gladly recommend their services."
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
    for review in LEGACY_REVIEWS:
        bind.execute(insert, {**review, "email": LEGACY_EMAIL})


def downgrade() -> None:
    bind = op.get_bind()
    for review in LEGACY_REVIEWS:
        bind.execute(sa.text("DELETE FROM reviews WHERE id = CAST(:id AS uuid)"), {"id": review["id"]})
