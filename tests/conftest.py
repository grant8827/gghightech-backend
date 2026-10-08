"""Shared test setup.

The environment is pinned here, before anything imports app.core.config,
because `settings = Settings()` is built at import time. Real environment
variables outrank the developer's local .env file, so these tests behave
the same on a laptop and in CI, and never read a real secret.

Nothing in this suite opens a database connection: DATABASE_URL points at a
closed port, and every test that reaches a DB-using dependency overrides
get_db with FakeSession below.
"""

import os
import secrets

os.environ["ENVIRONMENT"] = "production"
os.environ["ALLOW_DEV_AUTH_HEADERS"] = "false"
os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
os.environ["DATABASE_URL"] = "postgresql+psycopg2://test:test@127.0.0.1:1/never_connected"
# Blank, so no test can reach a paid or external service by accident. Tests
# that exercise the AI path set a dummy key and replace the HTTP call.
os.environ["OPENAI_API_KEY"] = ""
os.environ["MAILGUN_API_KEY"] = ""
os.environ["MAILGUN_DOMAIN"] = ""
os.environ["MAILGUN_FROM_EMAIL"] = ""
os.environ["LEAD_NOTIFICATION_EMAIL"] = "leads@gghightech.example"
os.environ["FRONTEND_URL"] = "http://localhost:3000"
os.environ["TRUSTED_PROXY_HOPS"] = "0"

import uuid  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.main import app as real_app  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services.ai_budget import ai_budget  # noqa: E402
from app.services.local_auth import create_access_token_for  # noqa: E402
from app.services.rate_limit import limiter  # noqa: E402

# Every signed-in request is checked against the users table, so a token
# is only usable in a test if its user "exists". Users made with
# make_user()/token_for() live here and are visible to every FakeSession,
# whichever one a test wires in. Emptied before each test.
_USERS = {}


def make_user(role="SUPER_ADMIN", email=None, org_id=None, user_id=None, **fields) -> User:
    user = User(
        id=user_id or uuid.uuid4(),
        org_id=org_id or uuid.uuid4(),
        email=email or f"{uuid.uuid4().hex[:8]}@example.com",
        full_name=fields.pop("full_name", "Test User"),
        role=role,
        password_hash=fields.pop("password_hash", "$2b$04$not-a-real-hash-but-non-empty"),
        token_version=fields.pop("token_version", 0),
        **fields,
    )
    _USERS[user.id] = user
    return user


def token_for(role="SUPER_ADMIN", **user_fields) -> str:
    """An access token for a freshly created user with that role."""
    return create_access_token_for(make_user(role, **user_fields))


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class _FakeQuery:
    """Just enough of a Query for `db.query(M).filter(M.col == value).first()`.
    `first` is called with that compared value and decides what comes back."""

    def __init__(self, first):
        self._first = first
        self._value = None

    def filter(self, *criteria, **kwargs):
        if criteria:
            self._value = getattr(getattr(criteria[0], "right", None), "value", None)
        return self

    def first(self):
        return self._first(self._value)


class FakeSession:
    """Stands in for a SQLAlchemy Session. Records the SQL text of every
    execute() so tests can assert on the RLS context that was applied,
    serves db.get() from a dict, and collects add()ed objects (filling in
    the id / created_at a real INSERT would have assigned)."""

    def __init__(self, objects=None):
        self.statements = []
        self.added = []
        self.commits = 0
        self._objects = objects or {}
        # model -> callable(filter_value) giving what .query(model).filter(...).first() yields
        self.first_results = {}

    def query(self, model):
        return _FakeQuery(self.first_results.get(model, lambda value: None))

    def execute(self, statement, *args, **kwargs):
        self.statements.append(str(statement))

    def get(self, model, key):
        if (model, key) in self._objects:
            return self._objects[(model, key)]
        if model is User and key in _USERS:
            return _USERS[key]
        return next((o for o in self.added if isinstance(o, model) and o.id == key), None)

    def delete(self, obj):
        _USERS.pop(getattr(obj, "id", None), None)
        self._objects = {k: v for k, v in self._objects.items() if v is not obj}

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if getattr(obj, "created_at", None) is None:
            obj.created_at = datetime.now(timezone.utc)
        self.added.append(obj)

    def flush(self):
        pass

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _default_db():
    """The real app gets a FakeSession by default, so a test that forgets
    to wire one in can't reach for a real database. Tests that need to
    inspect the session install their own override on top."""
    _USERS.clear()
    real_app.dependency_overrides[get_db] = lambda: FakeSession()
    yield
    real_app.dependency_overrides.pop(get_db, None)
    _USERS.clear()


@pytest.fixture(autouse=True)
def _fresh_abuse_counters():
    """The rate limiter and AI budget are process-wide; start every test
    from zero so tests can't affect each other."""
    limiter.reset()
    ai_budget.reset()
    yield
    limiter.reset()
    ai_budget.reset()


@pytest.fixture
def configure(monkeypatch):
    """Override fields on the live settings singleton for one test, e.g.
    configure(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS=True).
    Restored automatically afterwards."""

    def _configure(**overrides):
        for name, value in overrides.items():
            monkeypatch.setattr(settings, name, value)

    return _configure
