"""SEC-03 — the public estimate endpoints must be bounded.

Three layers are covered: the per-client sliding-window limiter, how the
client address is derived (X-Forwarded-For must not be spoofable), and the
global AI budget. No database, no network: get_db is replaced by
FakeSession and the OpenAI HTTP call by a local fake.
"""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.db.session import get_db
from app.main import app as real_app
from app.models.estimate import Estimate
from app.services import scope_analysis
from app.services.ai_budget import AiBudget, ai_budget
from app.services.rate_limit import SlidingWindowLimiter, client_ip
from app.services.scope_analysis import analyze_scope

from tests.conftest import FakeSession

PREVIEW = "/api/v1/estimates/preview"
CREATE = "/api/v1/estimates"

PREVIEW_BODY = {"project_type": "WEB_APPLICATION", "features": ["AUTH"], "design_tier": "STANDARD"}
CREATE_BODY = {
    **PREVIEW_BODY,
    "request_type": "NEW",
    "project_description": "We need a customer portal where our clients can sign in and review their invoices online.",
}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def client():
    return TestClient(real_app)


@pytest.fixture
def db():
    """The real app with its DB session replaced, for POST /estimates."""
    fake = FakeSession()
    real_app.dependency_overrides[get_db] = lambda: fake
    yield fake
    real_app.dependency_overrides.pop(get_db, None)


def estimates_saved(fake: FakeSession) -> int:
    return sum(isinstance(obj, Estimate) for obj in fake.added)


# --- Sliding-window limiter ---


def test_limiter_allows_up_to_the_limit_then_blocks():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    rules = [(3, 60)]
    assert [lim.hit("a", rules) for _ in range(3)] == [0.0, 0.0, 0.0]
    assert lim.hit("a", rules) == pytest.approx(60)


def test_limiter_window_slides():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    rules = [(2, 60)]
    lim.hit("a", rules)
    clock.advance(30)
    lim.hit("a", rules)
    assert lim.hit("a", rules) == pytest.approx(30)
    clock.advance(31)  # the first request has now aged out, the second has not
    assert lim.hit("a", rules) == 0.0
    assert lim.hit("a", rules) > 0


def test_limiter_blocked_requests_do_not_extend_the_block():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    rules = [(1, 60)]
    lim.hit("a", rules)
    for _ in range(50):
        clock.advance(1)
        assert lim.hit("a", rules) > 0
    clock.advance(11)  # 61s after the one request that was allowed
    assert lim.hit("a", rules) == 0.0


def test_limiter_enforces_every_rule():
    clock = FakeClock()
    lim = SlidingWindowLimiter(clock=clock)
    rules = [(2, 60), (3, 3600)]
    assert lim.hit("a", rules) == 0.0
    assert lim.hit("a", rules) == 0.0
    assert lim.hit("a", rules) > 0  # short window full
    clock.advance(61)
    assert lim.hit("a", rules) == 0.0  # third within the hour
    clock.advance(61)
    assert lim.hit("a", rules) == pytest.approx(3600 - 122)  # long window full


def test_limiter_tracks_clients_independently():
    lim = SlidingWindowLimiter(clock=FakeClock())
    rules = [(1, 60)]
    assert lim.hit("a", rules) == 0.0
    assert lim.hit("b", rules) == 0.0
    assert lim.hit("a", rules) > 0


def test_limiter_memory_is_bounded():
    lim = SlidingWindowLimiter(clock=FakeClock(), max_clients=100)
    for i in range(1000):
        lim.hit(f"client-{i}", [(5, 60)])
    assert len(lim._hits) == 100


# --- Client address ---


def make_request(peer="203.0.113.9", forwarded=None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "headers": headers, "client": (peer, 5555)})


def test_forwarded_header_is_ignored_by_default(configure):
    configure(TRUSTED_PROXY_HOPS=0)
    assert client_ip(make_request(forwarded="1.2.3.4")) == "203.0.113.9"


def test_one_trusted_proxy_uses_the_last_forwarded_entry(configure):
    configure(TRUSTED_PROXY_HOPS=1)
    assert client_ip(make_request(peer="10.0.0.1", forwarded="198.51.100.7")) == "198.51.100.7"


def test_client_supplied_forwarded_entries_are_never_trusted(configure):
    """A client behind one proxy sends its own X-Forwarded-For; the proxy
    appends the real address. Only what the proxy wrote counts."""
    configure(TRUSTED_PROXY_HOPS=1)
    request = make_request(peer="10.0.0.1", forwarded="1.1.1.1, 2.2.2.2, 198.51.100.7")
    assert client_ip(request) == "198.51.100.7"


def test_two_trusted_proxies_use_the_second_entry_from_the_right(configure):
    configure(TRUSTED_PROXY_HOPS=2)
    request = make_request(peer="10.0.0.1", forwarded="9.9.9.9, 198.51.100.7, 10.0.0.2")
    assert client_ip(request) == "198.51.100.7"


def test_missing_forwarded_header_falls_back_to_the_peer(configure):
    configure(TRUSTED_PROXY_HOPS=1)
    assert client_ip(make_request(peer="203.0.113.9")) == "203.0.113.9"


def test_ipv6_clients_are_grouped_by_slash_64(configure):
    configure(TRUSTED_PROXY_HOPS=0)
    a = client_ip(make_request(peer="2001:db8:abcd:12::1"))
    b = client_ip(make_request(peer="2001:db8:abcd:12:ffff:ffff:ffff:ffff"))
    c = client_ip(make_request(peer="2001:db8:abcd:13::1"))
    assert a == b == "2001:db8:abcd:12::"
    assert c != a


def test_ipv4_mapped_ipv6_is_treated_as_ipv4(configure):
    configure(TRUSTED_PROXY_HOPS=0)
    assert client_ip(make_request(peer="::ffff:203.0.113.9")) == "203.0.113.9"


def test_unparseable_address_shares_one_bucket(configure):
    configure(TRUSTED_PROXY_HOPS=1)
    assert client_ip(make_request(peer="10.0.0.1", forwarded="not-an-ip")) == "unknown"


# --- HTTP: preview endpoint ---


def test_preview_returns_429_with_retry_after_once_over_the_limit(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=3)
    assert [client.post(PREVIEW, json=PREVIEW_BODY).status_code for _ in range(3)] == [200, 200, 200]
    blocked = client.post(PREVIEW, json=PREVIEW_BODY)
    assert blocked.status_code == 429
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert "Too many requests" in blocked.json()["detail"]


def test_limit_of_zero_disables_that_limit(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=0)
    assert all(client.post(PREVIEW, json=PREVIEW_BODY).status_code == 200 for _ in range(25))


def test_invalid_requests_also_count_against_the_limit(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=2)
    assert client.post(PREVIEW, json={"nonsense": True}).status_code == 422
    assert client.post(PREVIEW, json={"nonsense": True}).status_code == 422
    assert client.post(PREVIEW, json=PREVIEW_BODY).status_code == 429


def test_different_clients_have_separate_allowances(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=1, TRUSTED_PROXY_HOPS=1)
    alice = {"X-Forwarded-For": "198.51.100.1"}
    bob = {"X-Forwarded-For": "198.51.100.2"}
    assert client.post(PREVIEW, json=PREVIEW_BODY, headers=alice).status_code == 200
    assert client.post(PREVIEW, json=PREVIEW_BODY, headers=alice).status_code == 429
    assert client.post(PREVIEW, json=PREVIEW_BODY, headers=bob).status_code == 200


def test_spoofed_forwarded_header_does_not_evade_the_limit(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=2, TRUSTED_PROXY_HOPS=0)
    statuses = [
        client.post(PREVIEW, json=PREVIEW_BODY, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
        for i in range(5)
    ]
    assert statuses == [200, 200, 429, 429, 429]


def test_spoofed_leading_entries_do_not_evade_the_limit_behind_a_proxy(client, configure):
    configure(ESTIMATE_PREVIEW_LIMIT_PER_MIN=2, TRUSTED_PROXY_HOPS=1)
    statuses = [
        client.post(
            PREVIEW, json=PREVIEW_BODY, headers={"X-Forwarded-For": f"198.51.100.{i}, 203.0.113.50"}
        ).status_code
        for i in range(4)
    ]
    assert statuses == [200, 200, 429, 429]


# --- HTTP: create endpoint ---


def test_create_is_limited_and_blocked_requests_write_nothing(client, db, configure):
    configure(ESTIMATE_CREATE_LIMIT_PER_10_MIN=2, ESTIMATE_CREATE_LIMIT_PER_DAY=40)
    assert client.post(CREATE, json=CREATE_BODY).status_code == 201
    assert client.post(CREATE, json=CREATE_BODY).status_code == 201
    assert estimates_saved(db) == 2
    commits_before = db.commits

    blocked = client.post(CREATE, json=CREATE_BODY)

    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers
    assert estimates_saved(db) == 2
    assert db.commits == commits_before


def test_create_daily_limit_applies_even_under_the_short_limit(client, db, configure):
    configure(ESTIMATE_CREATE_LIMIT_PER_10_MIN=100, ESTIMATE_CREATE_LIMIT_PER_DAY=3)
    statuses = [client.post(CREATE, json=CREATE_BODY).status_code for _ in range(5)]
    assert statuses == [201, 201, 201, 429, 429]
    assert estimates_saved(db) == 3


def test_create_and_preview_are_counted_separately(client, db, configure):
    configure(ESTIMATE_CREATE_LIMIT_PER_10_MIN=1, ESTIMATE_PREVIEW_LIMIT_PER_MIN=1)
    assert client.post(CREATE, json=CREATE_BODY).status_code == 201
    assert client.post(PREVIEW, json=PREVIEW_BODY).status_code == 200
    assert client.post(CREATE, json=CREATE_BODY).status_code == 429
    assert client.post(PREVIEW, json=PREVIEW_BODY).status_code == 429


def test_blocked_create_never_reaches_the_ai(client, db, configure, fake_openai):
    configure(ESTIMATE_CREATE_LIMIT_PER_10_MIN=1, OPENAI_API_KEY="test-key")
    assert client.post(CREATE, json=CREATE_BODY).status_code == 201
    assert client.post(CREATE, json=CREATE_BODY).status_code == 429
    assert fake_openai.calls == 1


def test_default_limits_are_on(configure):
    from app.core.config import Settings

    defaults = Settings.model_fields
    assert defaults["ESTIMATE_CREATE_LIMIT_PER_10_MIN"].default > 0
    assert defaults["ESTIMATE_CREATE_LIMIT_PER_DAY"].default > 0
    assert defaults["ESTIMATE_PREVIEW_LIMIT_PER_MIN"].default > 0
    assert defaults["AI_ANALYSIS_DAILY_CAP"].default > 0
    assert defaults["AI_ANALYSIS_MAX_CONCURRENT"].default > 0
    assert defaults["TRUSTED_PROXY_HOPS"].default == 0


# --- Global AI budget ---

AI_RESULT = {
    "summary": "A customer portal.",
    "complexity": "moderate",
    "adjustment_percent": 10,
    "detected_requirements": ["Authentication"],
    "risks": ["Scope to be confirmed."],
    "market_comparison": "Typical.",
    "recommended_price_min": 12000,
    "recommended_price_max": 20000,
    "recommended_weeks_min": 6,
    "recommended_weeks_max": 10,
    "recommended_monthly_min": 0,
    "recommended_monthly_max": 0,
    "recommended_hours_per_week_min": 0,
    "recommended_hours_per_week_max": 0,
}


class FakeOpenAI:
    """Replaces httpx.post inside scope_analysis. Counts calls; can be told
    to fail, or to run a callback while a call is 'in flight'."""

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.during_call = None

    def __call__(self, url, **kwargs):
        self.calls += 1
        if self.during_call:
            self.during_call()
        if self.fail:
            raise RuntimeError("simulated OpenAI outage")
        return self

    def raise_for_status(self):
        pass

    def json(self):
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(AI_RESULT)}]}]}


@pytest.fixture
def fake_openai(monkeypatch):
    fake = FakeOpenAI()
    monkeypatch.setattr(scope_analysis.httpx, "post", fake)
    return fake


def analyze():
    return analyze_scope(CREATE_BODY["project_description"], "NEW", "WEB_APPLICATION", ["AUTH"], "STANDARD")


def test_without_an_api_key_the_ai_is_never_called(configure, fake_openai):
    configure(OPENAI_API_KEY="")
    assert analyze().source == "rules"
    assert fake_openai.calls == 0
    assert ai_budget.used_today == 0


def test_within_budget_the_ai_result_is_used(configure, fake_openai):
    configure(OPENAI_API_KEY="test-key")
    result = analyze()
    assert result.source == "openai"
    assert result.recommended_price_min == 12000
    assert ai_budget.used_today == 1


def test_daily_cap_stops_ai_calls_and_falls_back_to_rules(configure, fake_openai):
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_DAILY_CAP=3)
    sources = [analyze().source for _ in range(6)]
    assert sources == ["openai", "openai", "openai", "rules", "rules", "rules"]
    assert fake_openai.calls == 3


def test_daily_cap_of_zero_turns_ai_off(configure, fake_openai):
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_DAILY_CAP=0)
    assert analyze().source == "rules"
    assert fake_openai.calls == 0


def test_failed_ai_calls_still_count_against_the_daily_cap(configure, fake_openai):
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_DAILY_CAP=2)
    fake_openai.fail = True
    assert [analyze().source for _ in range(4)] == ["rules"] * 4
    assert fake_openai.calls == 2


def test_concurrency_cap_sends_overflow_to_rules(configure, fake_openai):
    """While one AI call is in flight (cap 1), a second request must not
    start another — it gets the rules-based result immediately."""
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_MAX_CONCURRENT=1)
    overflow = []

    def second_request_arrives():
        fake_openai.during_call = None
        overflow.append(analyze())

    fake_openai.during_call = second_request_arrives
    first = analyze()

    assert first.source == "openai"
    assert overflow[0].source == "rules"
    assert fake_openai.calls == 1


def test_concurrency_slot_is_released_after_success_and_after_failure(configure, fake_openai):
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_MAX_CONCURRENT=1)
    assert analyze().source == "openai"
    fake_openai.fail = True
    assert analyze().source == "rules"
    fake_openai.fail = False
    assert analyze().source == "openai"
    assert fake_openai.calls == 3


def test_daily_cap_resets_on_a_new_utc_day(configure):
    configure(AI_ANALYSIS_DAILY_CAP=1, AI_ANALYSIS_MAX_CONCURRENT=5)
    today = [date(2026, 10, 7)]
    budget = AiBudget(today=lambda: today[0])

    assert budget.try_acquire() is True
    budget.release()
    assert budget.try_acquire() is False

    today[0] = date(2026, 10, 8)
    assert budget.try_acquire() is True


def test_ai_budget_bounds_cost_across_many_client_addresses(client, db, configure, fake_openai):
    """The scenario per-IP limiting can't stop: many addresses, one
    request each. Every request still gets an estimate; only the capped
    number reach the paid API."""
    configure(OPENAI_API_KEY="test-key", AI_ANALYSIS_DAILY_CAP=5, TRUSTED_PROXY_HOPS=1)
    statuses = [
        client.post(CREATE, json=CREATE_BODY, headers={"X-Forwarded-For": f"198.51.100.{i}"}).status_code
        for i in range(30)
    ]
    assert statuses == [201] * 30
    assert fake_openai.calls == 5
    sources = [e.scope_configuration["analysis"]["source"] for e in db.added if isinstance(e, Estimate)]
    assert sources.count("openai") == 5
    assert sources.count("rules") == 25
