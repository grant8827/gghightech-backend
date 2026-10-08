"""SEC-05 — sign-in, accept-invite and bootstrap must not accept unlimited
guesses.

Runs against the real app with the DB replaced by FakeSession and the
limiter's clock replaced by a controllable one. One real account exists
(REAL_EMAIL / REAL_PASSWORD); its hash uses a cheap bcrypt cost so the
suite stays fast.
"""

import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient

from app.db.session import get_db
from app.main import app as real_app
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.local_auth import create_invite_token
from app.services.rate_limit import limiter

from tests.conftest import FakeSession

LOGIN = "/api/v1/auth/login"
ACCEPT_INVITE = "/api/v1/auth/accept-invite"
BOOTSTRAP = "/api/v1/auth/bootstrap-superadmin"

REAL_EMAIL = "pm@gghightech.example"
REAL_PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "definitely-not-it"
GHOST_EMAIL = "nobody@nowhere.example"
BOOTSTRAP_TOKEN = "bootstrap-" + "t" * 40


class FakeClock:
    def __init__(self):
        self.now = 10_000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(limiter, "_clock", fake)
    return fake


@pytest.fixture
def db():
    user = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email=REAL_EMAIL,
        full_name="Pat Manager",
        role="PROJECT_MANAGER",
        token_version=0,
        password_hash=bcrypt.hashpw(REAL_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode(),
    )
    fake = FakeSession()
    fake.user = user
    fake.first_results[User] = lambda email: user if email == REAL_EMAIL else None
    real_app.dependency_overrides[get_db] = lambda: fake
    yield fake
    real_app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def client(db, clock):
    return TestClient(real_app)


def attempt(client, email=REAL_EMAIL, password=WRONG_PASSWORD, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    return client.post(LOGIN, json={"email": email, "password": password}, headers=headers)


def audit_rows(db):
    return [row for row in db.added if isinstance(row, AuditLog)]


# --- Lockout per (email, IP) ---


def test_correct_password_signs_in(client):
    response = attempt(client, password=REAL_PASSWORD)
    assert response.status_code == 200
    assert response.json()["role"] == "PROJECT_MANAGER"


def test_account_locks_after_repeated_wrong_passwords(client):
    assert [attempt(client).status_code for _ in range(5)] == [401] * 5
    locked = attempt(client)
    assert locked.status_code == 429
    assert 1 <= int(locked.headers["Retry-After"]) <= 15 * 60
    assert "Too many failed sign-in attempts" in locked.json()["detail"]


def test_correct_password_is_refused_while_locked(client):
    for _ in range(5):
        attempt(client)
    assert attempt(client, password=REAL_PASSWORD).status_code == 429


def test_lockout_expires_and_sign_in_works_again(client, clock):
    for _ in range(5):
        attempt(client)
    clock.advance(15 * 60 - 1)
    assert attempt(client, password=REAL_PASSWORD).status_code == 429
    clock.advance(2)
    assert attempt(client, password=REAL_PASSWORD).status_code == 200


def test_hammering_while_locked_does_not_extend_the_lockout(client, clock):
    for _ in range(5):
        attempt(client)
    # Stay under the per-IP attempt limit so this isolates the lockout.
    for _ in range(10):
        clock.advance(60)
        assert attempt(client).status_code == 429
    clock.advance(5 * 60 + 1)  # 15 min after the last counted failure
    assert attempt(client, password=REAL_PASSWORD).status_code == 200


def test_successful_sign_in_clears_earlier_strikes(client):
    for _ in range(4):
        assert attempt(client).status_code == 401
    assert attempt(client, password=REAL_PASSWORD).status_code == 200
    assert [attempt(client).status_code for _ in range(4)] == [401] * 4
    assert attempt(client, password=REAL_PASSWORD).status_code == 200


def test_strikes_older_than_the_window_do_not_count(client, clock):
    for _ in range(4):
        attempt(client)
    clock.advance(15 * 60 + 1)
    assert [attempt(client).status_code for _ in range(4)] == [401] * 4


def test_email_is_matched_case_insensitively_for_lockout(client):
    for email in ("PM@gghightech.example", "Pm@GGHighTech.example", REAL_EMAIL, "pM@gghightech.example", REAL_EMAIL):
        attempt(client, email=email)
    assert attempt(client, password=REAL_PASSWORD).status_code == 429


def test_lockout_threshold_follows_settings(client, configure):
    configure(LOGIN_FAILURES_BEFORE_LOCKOUT=2)
    assert [attempt(client).status_code for _ in range(3)] == [401, 401, 429]


def test_zero_disables_the_lockouts(client, configure):
    configure(LOGIN_FAILURES_BEFORE_LOCKOUT=0, LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT=0)
    assert [attempt(client).status_code for _ in range(12)] == [401] * 12
    assert attempt(client, password=REAL_PASSWORD).status_code == 200


# --- No account enumeration ---


def test_unknown_email_behaves_exactly_like_a_real_one(client):
    real = [attempt(client, email=REAL_EMAIL) for _ in range(6)]
    limiter.reset()
    ghost = [attempt(client, email=GHOST_EMAIL) for _ in range(6)]
    assert [r.status_code for r in real] == [r.status_code for r in ghost] == [401] * 5 + [429]
    assert [r.json() for r in real] == [r.json() for r in ghost]


# --- Scope of a lockout ---


def test_lockout_on_one_account_does_not_lock_another_from_the_same_address(client):
    for _ in range(5):
        attempt(client, email=GHOST_EMAIL)
    assert attempt(client, email=GHOST_EMAIL).status_code == 429
    assert attempt(client, password=REAL_PASSWORD).status_code == 200


def test_attacker_address_lockout_does_not_lock_the_real_user_elsewhere(client, configure):
    configure(TRUSTED_PROXY_HOPS=1)
    for _ in range(5):
        attempt(client, ip="198.51.100.66")
    assert attempt(client, ip="198.51.100.66", password=REAL_PASSWORD).status_code == 429
    assert attempt(client, ip="203.0.113.10", password=REAL_PASSWORD).status_code == 200


def test_guessing_spread_across_many_addresses_locks_the_account(client, configure):
    """Per-(email, IP) alone can't stop this: each address stays under its
    own threshold. The any-address count does."""
    configure(TRUSTED_PROXY_HOPS=1, LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT=8)
    statuses = [attempt(client, ip=f"198.51.100.{i}").status_code for i in range(8)]
    assert statuses == [401] * 8
    assert attempt(client, ip="198.51.100.200").status_code == 429
    assert attempt(client, ip="203.0.113.10", password=REAL_PASSWORD).status_code == 429


def test_good_sign_in_does_not_reset_the_any_address_count(client, configure):
    configure(TRUSTED_PROXY_HOPS=1, LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT=8)
    for i in range(7):
        attempt(client, ip=f"198.51.100.{i}")
    assert attempt(client, ip="203.0.113.10", password=REAL_PASSWORD).status_code == 200
    assert attempt(client, ip="198.51.100.99").status_code == 401  # the 8th failure
    assert attempt(client, ip="198.51.100.100").status_code == 429


# --- Attempts per IP address ---


def test_one_address_cannot_spray_many_accounts(client, configure):
    configure(LOGIN_ATTEMPTS_PER_IP_PER_5_MIN=4)
    statuses = [attempt(client, email=f"user{i}@example.com").status_code for i in range(6)]
    assert statuses == [401, 401, 401, 401, 429, 429]


def test_ip_limit_counts_successful_sign_ins_too(client, configure):
    configure(LOGIN_ATTEMPTS_PER_IP_PER_5_MIN=3)
    statuses = [attempt(client, password=REAL_PASSWORD).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]


def test_ip_limit_is_per_address(client, configure):
    configure(LOGIN_ATTEMPTS_PER_IP_PER_5_MIN=1, TRUSTED_PROXY_HOPS=1)
    assert attempt(client, ip="198.51.100.1", password=REAL_PASSWORD).status_code == 200
    assert attempt(client, ip="198.51.100.1", password=REAL_PASSWORD).status_code == 429
    assert attempt(client, ip="198.51.100.2", password=REAL_PASSWORD).status_code == 200


# --- Audit trail ---


def test_failed_attempts_are_audited_without_the_password(client, db):
    attempt(client)
    (row,) = audit_rows(db)
    assert row.action == "auth.login_failed"
    assert row.actor_role == "ANONYMOUS"
    assert row.resource_id == str(db.user.id)
    assert row.org_id == db.user.org_id
    assert row.event_metadata["email"] == REAL_EMAIL
    assert "ip" in row.event_metadata
    assert WRONG_PASSWORD not in repr(vars(row))
    assert db.commits == 1


def test_failed_attempt_for_unknown_email_is_audited_with_no_user_link(client, db):
    attempt(client, email=GHOST_EMAIL)
    (row,) = audit_rows(db)
    assert row.action == "auth.login_failed"
    assert row.resource_id is None
    assert row.event_metadata["email"] == GHOST_EMAIL


def test_the_attempt_that_triggers_a_lockout_is_audited_as_such(client, db):
    for _ in range(5):
        attempt(client)
    assert [row.action for row in audit_rows(db)] == ["auth.login_failed"] * 4 + ["auth.login_locked"]


def test_attempts_refused_while_locked_write_nothing(client, db):
    for _ in range(5):
        attempt(client)
    rows_before, commits_before = len(audit_rows(db)), db.commits
    for _ in range(5):
        assert attempt(client).status_code == 429
    assert len(audit_rows(db)) == rows_before
    assert db.commits == commits_before


def test_successful_sign_in_writes_no_failure_row(client, db):
    attempt(client, password=REAL_PASSWORD)
    assert audit_rows(db) == []


# --- accept-invite ---


def test_accept_invite_is_rate_limited_per_address(client):
    body = {"token": "not-a-real-invite", "password": "a-long-enough-password"}
    statuses = [client.post(ACCEPT_INVITE, json=body).status_code for _ in range(12)]
    assert statuses == [401] * 10 + [429, 429]


def test_valid_invite_still_works_within_the_limit(client, db):
    invited = User(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email="new@client.example",
        full_name="New Client",
        role="CLIENT_ADMIN",
        token_version=0,
    )
    db._objects[(User, invited.id)] = invited
    body = {"token": create_invite_token(str(invited.id)), "password": "a-long-enough-password"}
    response = client.post(ACCEPT_INVITE, json=body)
    assert response.status_code == 200
    assert invited.password_hash


# --- bootstrap ---


def bootstrap(client, token):
    return client.post(
        BOOTSTRAP,
        params={"full_name": "Root Admin"},
        json={"email": "root@gghightech.example", "password": "a-long-enough-password"},
        headers={"X-Bootstrap-Token": token},
    )


def test_disabled_bootstrap_always_looks_like_a_missing_route(client, configure):
    configure(BOOTSTRAP_TOKEN="")
    assert [bootstrap(client, "guess").status_code for _ in range(12)] == [404] * 12


def test_enabled_bootstrap_limits_token_guesses(client, configure):
    configure(BOOTSTRAP_TOKEN=BOOTSTRAP_TOKEN)
    statuses = [bootstrap(client, f"guess-{i}").status_code for i in range(7)]
    assert statuses == [404] * 5 + [429, 429]


def test_correct_bootstrap_token_is_refused_once_guesses_are_used_up(client, configure):
    configure(BOOTSTRAP_TOKEN=BOOTSTRAP_TOKEN)
    for i in range(5):
        bootstrap(client, f"guess-{i}")
    assert bootstrap(client, BOOTSTRAP_TOKEN).status_code == 429


# --- Defaults ---


def test_default_protection_is_on():
    from app.core.config import Settings

    defaults = Settings.model_fields
    assert defaults["LOGIN_ATTEMPTS_PER_IP_PER_5_MIN"].default > 0
    assert 0 < defaults["LOGIN_FAILURES_BEFORE_LOCKOUT"].default <= 10
    assert defaults["LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT"].default > 0
    assert defaults["LOGIN_LOCKOUT_MINUTES"].default >= 5
