"""Client reviews — public submit/read and staff moderation.

Unlike the rest of the suite these run real SQL, against an in-memory
SQLite database, because what matters here is what the queries return:
which reviews are public, in what order, and what the average is. Only
the tables the review routes touch are created.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.organization import Organization
from app.models.review import Review
from app.models.user import User
from app.services.local_auth import create_access_token_for

from tests.conftest import bearer

REVIEWS = "/api/v1/reviews"
VALID = {
    "name": "Jane Doe",
    "email": "jane@client.example",
    "rating": 5,
    "message": "GG HighTech delivered our portal on time and it works beautifully.",
}


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


class _SqliteSession(Session):
    """require_roles issues Postgres-only `SET LOCAL app.bypass_rls`
    statements (row-level security). They mean nothing to SQLite and are
    irrelevant to reviews, which aren't RLS-protected — skip them."""

    def execute(self, statement, *args, **kwargs):
        if str(statement).lstrip().upper().startswith("SET LOCAL"):
            return None
        return super().execute(statement, *args, **kwargs)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for model in (Organization, User, Review, AuditLog):
        model.__table__.create(engine)
    session = sessionmaker(bind=engine, class_=_SqliteSession, expire_on_commit=False)()
    real_app.dependency_overrides[get_db] = lambda: session
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(db):
    return TestClient(real_app)


_clock = [datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)]


def add_review(db, rating=5, status="PUBLISHED", featured=False, name="Reviewer", message="A perfectly fine review."):
    """Inserted directly, each one a minute newer than the last."""
    _clock[0] += timedelta(minutes=1)
    review = Review(
        id=uuid.uuid4(),
        name=name,
        email=f"{uuid.uuid4().hex[:8]}@client.example",
        rating=rating,
        message=message,
        status=status,
        is_featured=featured,
        created_at=_clock[0],
    )
    db.add(review)
    db.commit()
    return review


def staff(db, role="SUPER_ADMIN") -> dict:
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=f"{role.lower()}-{uuid.uuid4().hex[:6]}@gghightech.example",
        full_name="Staff Member",
        role=role,
        password_hash="$2b$04$not-a-real-hash-but-non-empty",
        token_version=0,
    )
    db.add(user)
    db.commit()
    return bearer(create_access_token_for(user))


def submit(client, ip=None, **overrides):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post(REVIEWS, json={**VALID, **overrides}, headers=headers)


# --- Submitting ---


def test_a_submitted_review_is_public_immediately_with_no_verification(client, db):
    response = submit(client)
    assert response.status_code == 201
    assert response.json() == {"status": "PUBLISHED"}

    (listed,) = client.get(REVIEWS).json()
    assert listed["name"] == "Jane Doe"
    assert listed["rating"] == 5
    assert listed["message"] == VALID["message"]


def test_public_responses_never_contain_the_email(client, db):
    submit(client)
    for url in (REVIEWS, f"{REVIEWS}/highlights", f"{REVIEWS}/summary"):
        body = client.get(url).text
        assert "jane@client.example" not in body
        assert "email" not in body
    assert "jane@client.example" not in submit(client, email="other@client.example").text


def test_email_is_stored_lowercased_for_staff(client, db):
    submit(client, email="Jane.Doe@Client.Example")
    assert db.query(Review).one().email == "jane.doe@client.example"


def test_submission_is_audited(client, db):
    submit(client)
    (row,) = db.query(AuditLog).all()
    assert row.action == "review.create"
    assert row.actor_role == "ANONYMOUS"
    assert row.event_metadata == {"rating": 5, "status": "PUBLISHED"}


def test_with_auto_publish_off_a_new_review_waits_for_approval(client, db, configure):
    configure(REVIEWS_AUTO_PUBLISH=False)
    assert submit(client).json() == {"status": "PENDING"}
    assert client.get(REVIEWS).json() == []
    assert client.get(f"{REVIEWS}/summary").json()["count"] == 0
    assert db.query(Review).one().status == "PENDING"


# --- Validation ---


@pytest.mark.parametrize(
    "field, value",
    [
        ("rating", 0),
        ("rating", 6),
        ("rating", 4.5),
        ("rating", "five"),
        ("name", ""),
        ("name", " J "),
        ("name", "x" * 81),
        ("email", "not-an-email"),
        ("email", ""),
        ("message", ""),
        ("message", "Too short"),
        ("message", "x" * 1501),
        ("message", "Great work! Visit https://spam.example for cheap pills"),
        ("message", "Great work! See www.spam.example for more of the same"),
        ("name", "www.spam.example"),
    ],
)
def test_invalid_submissions_are_rejected_and_nothing_is_saved(client, db, field, value):
    assert submit(client, **{field: value}).status_code == 422
    assert db.query(Review).count() == 0


@pytest.mark.parametrize("missing", ["name", "email", "rating", "message"])
def test_every_field_is_required(client, db, missing):
    body = {k: v for k, v in VALID.items() if k != missing}
    assert client.post(REVIEWS, json=body).status_code == 422


def test_technology_names_with_dots_are_not_mistaken_for_links(client, db):
    message = "They rebuilt our ASP.NET system with Node.js and Socket.io and it runs great."
    assert submit(client, message=message).status_code == 201


def test_text_is_tidied_but_line_breaks_are_kept(client, db):
    submit(client, name="  Jane    Doe ", message="  Great   team.\n\n\n  Would hire\tagain.  ")
    review = db.query(Review).one()
    assert review.name == "Jane Doe"
    assert review.message == "Great team.\nWould hire again."


def test_html_is_stored_as_plain_text_not_interpreted(client, db):
    message = "<script>alert('x')</script> honestly a great experience overall"
    assert submit(client, message=message).status_code == 201
    assert client.get(REVIEWS).json()[0]["message"] == message  # the frontend escapes it on render


def test_submitter_cannot_set_status_or_featured(client, db, configure):
    configure(REVIEWS_AUTO_PUBLISH=False)
    client.post(REVIEWS, json={**VALID, "status": "PUBLISHED", "is_featured": True, "id": str(uuid.uuid4())})
    review = db.query(Review).one()
    assert review.status == "PENDING"
    assert review.is_featured is False


# --- Spam protection ---


def test_spam_trap_reports_success_but_saves_nothing(client, db):
    response = submit(client, website="http://bot.example")
    assert response.status_code == 201
    assert response.json() == {"status": "PUBLISHED"}  # indistinguishable from a real success
    assert db.query(Review).count() == 0
    assert db.query(AuditLog).count() == 0


def test_submissions_are_rate_limited_per_address(client, db, configure):
    configure(REVIEW_SUBMIT_LIMIT_PER_HOUR=3)
    statuses = [submit(client, email=f"person{i}@client.example").status_code for i in range(5)]
    assert statuses == [201, 201, 201, 429, 429]
    assert db.query(Review).count() == 3


def test_rate_limit_is_per_address(client, db, configure):
    configure(REVIEW_SUBMIT_LIMIT_PER_HOUR=1, TRUSTED_PROXY_HOPS=1)
    assert submit(client, ip="198.51.100.1", email="a@client.example").status_code == 201
    assert submit(client, ip="198.51.100.1", email="b@client.example").status_code == 429
    assert submit(client, ip="198.51.100.2", email="c@client.example").status_code == 201


def test_one_email_cannot_stack_reviews_from_many_addresses(client, db, configure):
    configure(TRUSTED_PROXY_HOPS=1)
    statuses = [submit(client, ip=f"198.51.100.{i}", email="Same@Client.Example").status_code for i in range(4)]
    assert statuses == [201, 201, 429, 429]
    assert db.query(Review).count() == 2


# --- What the public sees ---


def test_only_published_reviews_are_listed(client, db):
    add_review(db, name="Shown")
    add_review(db, name="Waiting", status="PENDING")
    add_review(db, name="Taken down", status="HIDDEN")
    assert [r["name"] for r in client.get(REVIEWS).json()] == ["Shown"]


def test_list_puts_featured_first_then_newest(client, db):
    add_review(db, name="oldest")
    add_review(db, name="featured", featured=True)
    add_review(db, name="newest")
    assert [r["name"] for r in client.get(REVIEWS).json()] == ["featured", "newest", "oldest"]


def test_list_is_paginated_and_capped(client, db):
    for i in range(5):
        add_review(db, name=f"r{i}")
    assert [r["name"] for r in client.get(REVIEWS, params={"limit": 2}).json()] == ["r4", "r3"]
    assert [r["name"] for r in client.get(REVIEWS, params={"limit": 2, "offset": 2}).json()] == ["r2", "r1"]
    assert client.get(REVIEWS, params={"limit": 101}).status_code == 422


def test_highlights_are_the_featured_reviews(client, db):
    add_review(db, name="plain five", rating=5)
    add_review(db, name="featured three", rating=3, featured=True)
    assert [r["name"] for r in client.get(f"{REVIEWS}/highlights").json()] == ["featured three"]


def test_highlights_fall_back_to_recent_good_reviews_until_something_is_featured(client, db):
    add_review(db, name="five", rating=5)
    add_review(db, name="one", rating=1)
    add_review(db, name="four", rating=4)
    add_review(db, name="three", rating=3)
    add_review(db, name="hidden five", rating=5, status="HIDDEN")
    assert [r["name"] for r in client.get(f"{REVIEWS}/highlights").json()] == ["four", "five"]


def test_highlights_respect_the_limit(client, db):
    for i in range(6):
        add_review(db, name=f"f{i}", featured=True)
    assert len(client.get(f"{REVIEWS}/highlights").json()) == 3
    assert len(client.get(f"{REVIEWS}/highlights", params={"limit": 5}).json()) == 5


# --- Average rating ---


def test_summary_with_no_reviews(client, db):
    assert client.get(f"{REVIEWS}/summary").json() == {
        "average": None,
        "count": 0,
        "breakdown": {"5": 0, "4": 0, "3": 0, "2": 0, "1": 0},
    }


def test_average_counts_only_published_reviews(client, db):
    for rating in (5, 5, 4, 2):
        add_review(db, rating=rating)
    add_review(db, rating=1, status="HIDDEN")
    add_review(db, rating=1, status="PENDING")

    summary = client.get(f"{REVIEWS}/summary").json()

    assert summary["count"] == 4
    assert summary["average"] == 4.0
    assert summary["breakdown"] == {"5": 2, "4": 1, "3": 0, "2": 1, "1": 0}


def test_average_is_rounded_to_one_decimal(client, db):
    for rating in (5, 5, 4):
        add_review(db, rating=rating)
    assert client.get(f"{REVIEWS}/summary").json()["average"] == 4.7


def test_hiding_a_review_changes_the_average(client, db):
    add_review(db, rating=5)
    low = add_review(db, rating=1)
    assert client.get(f"{REVIEWS}/summary").json()["average"] == 3.0
    client.patch(f"{REVIEWS}/{low.id}", json={"status": "HIDDEN"}, headers=staff(db))
    assert client.get(f"{REVIEWS}/summary").json() == {
        "average": 5.0,
        "count": 1,
        "breakdown": {"5": 1, "4": 0, "3": 0, "2": 0, "1": 0},
    }


# --- Staff moderation ---


def test_staff_see_every_review_with_email_and_status(client, db):
    add_review(db, name="shown")
    add_review(db, name="hidden", status="HIDDEN")
    add_review(db, name="pending", status="PENDING")

    rows = client.get(f"{REVIEWS}/admin", headers=staff(db)).json()

    assert [r["name"] for r in rows] == ["pending", "hidden", "shown"]
    assert all("@client.example" in r["email"] for r in rows)
    assert {r["status"] for r in rows} == {"PUBLISHED", "HIDDEN", "PENDING"}


def test_staff_can_filter_by_status(client, db):
    add_review(db, name="shown")
    add_review(db, name="hidden", status="HIDDEN")
    headers = staff(db)
    assert [r["name"] for r in client.get(f"{REVIEWS}/admin", params={"status": "HIDDEN"}, headers=headers).json()] == ["hidden"]
    assert client.get(f"{REVIEWS}/admin", params={"status": "NOPE"}, headers=headers).status_code == 422


def test_hide_then_republish(client, db):
    review = add_review(db)
    headers = staff(db, "PROJECT_MANAGER")

    hidden = client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}, headers=headers)
    assert hidden.status_code == 200
    assert hidden.json()["status"] == "HIDDEN"
    assert hidden.json()["moderated_by"].startswith("project_manager-")
    assert hidden.json()["moderated_at"]
    assert client.get(REVIEWS).json() == []

    client.patch(f"{REVIEWS}/{review.id}", json={"status": "PUBLISHED"}, headers=headers)
    assert len(client.get(REVIEWS).json()) == 1


def test_publishing_a_pending_review_makes_it_public(client, db):
    review = add_review(db, status="PENDING")
    client.patch(f"{REVIEWS}/{review.id}", json={"status": "PUBLISHED"}, headers=staff(db))
    assert [r["id"] for r in client.get(REVIEWS).json()] == [str(review.id)]


def test_feature_and_unfeature(client, db):
    review = add_review(db, rating=3)
    headers = staff(db)
    assert client.patch(f"{REVIEWS}/{review.id}", json={"is_featured": True}, headers=headers).json()["is_featured"] is True
    assert [r["id"] for r in client.get(f"{REVIEWS}/highlights").json()] == [str(review.id)]
    assert client.patch(f"{REVIEWS}/{review.id}", json={"is_featured": False}, headers=headers).json()["is_featured"] is False


@pytest.mark.parametrize("status", ["HIDDEN", "PENDING"])
def test_a_review_that_is_not_public_cannot_be_featured(client, db, status):
    review = add_review(db, status=status)
    headers = staff(db)
    assert client.patch(f"{REVIEWS}/{review.id}", json={"is_featured": True}, headers=headers).status_code == 422
    both = client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN", "is_featured": True}, headers=headers)
    assert both.status_code == 422


def test_publish_and_feature_in_one_step(client, db):
    review = add_review(db, status="PENDING")
    response = client.patch(
        f"{REVIEWS}/{review.id}", json={"status": "PUBLISHED", "is_featured": True}, headers=staff(db)
    )
    assert response.json()["status"] == "PUBLISHED"
    assert response.json()["is_featured"] is True


def test_hiding_a_featured_review_unfeatures_it(client, db):
    review = add_review(db, featured=True)
    response = client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}, headers=staff(db))
    assert response.json()["is_featured"] is False
    assert client.get(f"{REVIEWS}/highlights").json() == []


def test_moderation_rejects_unknown_status_and_unknown_review(client, db):
    review = add_review(db)
    headers = staff(db)
    assert client.patch(f"{REVIEWS}/{review.id}", json={"status": "APPROVED"}, headers=headers).status_code == 422
    assert client.patch(f"{REVIEWS}/{uuid.uuid4()}", json={"status": "HIDDEN"}, headers=headers).status_code == 404


def test_moderation_cannot_rewrite_the_review_itself(client, db):
    review = add_review(db, rating=1, name="Unhappy", message="It did not go well for us at all.")
    client.patch(
        f"{REVIEWS}/{review.id}",
        json={"rating": 5, "name": "Happy", "message": "Wonderful!", "email": "x@y.example"},
        headers=staff(db),
    )
    db.refresh(review)
    assert (review.rating, review.name, review.message) == (1, "Unhappy", "It did not go well for us at all.")


def test_moderation_and_deletion_are_audited(client, db):
    review = add_review(db)
    headers = staff(db)
    client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}, headers=headers)
    client.delete(f"{REVIEWS}/{review.id}", headers=headers)
    actions = [row.action for row in db.query(AuditLog).order_by(AuditLog.created_at).all()]
    assert sorted(actions) == ["review.delete", "review.moderate"]


def test_delete_removes_the_review_for_good(client, db):
    review = add_review(db)
    assert client.delete(f"{REVIEWS}/{review.id}", headers=staff(db)).status_code == 204
    assert db.query(Review).count() == 0
    assert client.get(REVIEWS).json() == []
    assert client.delete(f"{REVIEWS}/{review.id}", headers=staff(db)).status_code == 404


# --- Who may moderate ---


@pytest.mark.parametrize("role", ["LEAD_ENGINEER", "CLIENT_ADMIN", "CLIENT_VIEWER"])
def test_other_roles_cannot_moderate(client, db, role):
    review = add_review(db)
    headers = staff(db, role)
    assert client.get(f"{REVIEWS}/admin", headers=headers).status_code == 403
    assert client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}, headers=headers).status_code == 403
    assert client.delete(f"{REVIEWS}/{review.id}", headers=headers).status_code == 403
    assert db.query(Review).one().status == "PUBLISHED"


def test_project_manager_can_moderate_but_not_delete(client, db):
    review = add_review(db)
    headers = staff(db, "PROJECT_MANAGER")
    assert client.get(f"{REVIEWS}/admin", headers=headers).status_code == 200
    assert client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}, headers=headers).status_code == 200
    assert client.delete(f"{REVIEWS}/{review.id}", headers=headers).status_code == 403
    assert db.query(Review).count() == 1


def test_anonymous_cannot_moderate_or_see_the_staff_list(client, db):
    review = add_review(db)
    assert client.get(f"{REVIEWS}/admin").status_code == 401
    assert client.patch(f"{REVIEWS}/{review.id}", json={"status": "HIDDEN"}).status_code == 401
    assert client.delete(f"{REVIEWS}/{review.id}").status_code == 401
    assert db.query(Review).one().status == "PUBLISHED"
