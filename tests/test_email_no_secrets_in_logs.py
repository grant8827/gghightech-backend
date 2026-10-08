"""SEC-08 — the links these emails carry are credentials, and lead
notifications carry a prospect's contact details. None of it may reach the
log; it goes to the recipient's inbox and nowhere else.

The Mailgun HTTP call is replaced by a local recorder. conftest.py blanks
the Mailgun settings for the whole suite, so nothing here (or anywhere
else in the tests) can send a real email.
"""

import logging
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.session import get_db
from app.main import app as real_app
from app.models.organization import Organization
from app.services import email as email_service
from app.services.email import (
    mask_email,
    send_invite_email,
    send_lead_notification,
    send_payment_link_email,
)

from tests.conftest import FakeSession, bearer, token_for

API_KEY = "key-" + "k" * 40
INVITE_LINK = "https://app.gghightech.example/accept-invite?token=eyJhbGciOi.INVITE-SECRET.signature"
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_live_PAYMENT-SECRET"
LEAD_EMAIL = "prospect@bigclient.example"
LEAD_PHONE = "555-010-7788"
BRIEF = "We need a customer portal where our clients can sign in and review their invoices online."


class Outbox:
    """Stands in for httpx.post. Records each send; can simulate failures."""

    def __init__(self):
        self.sent = []
        self.status_code = 200
        self.raises = None

    def __call__(self, url, **kwargs):
        self.sent.append({"url": url, **kwargs})
        if self.raises:
            raise self.raises
        return self

    @property
    def last(self):
        return self.sent[-1]


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(email_service.httpx, "post", box)
    return box


@pytest.fixture
def mailgun(configure, outbox):
    """Email configured, in production."""
    configure(
        ENVIRONMENT="production",
        MAILGUN_API_KEY=API_KEY,
        MAILGUN_DOMAIN="mg.gghightech.example",
        MAILGUN_API_BASE="https://api.mailgun.net",
        MAILGUN_FROM_EMAIL="GG HighTech <hello@gghightech.example>",
        LEAD_NOTIFICATION_EMAIL="leads@gghightech.example",
    )
    return outbox


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def assert_clean(logs, *secrets):
    text = logs.text
    for secret in secrets:
        assert secret not in text, f"{secret!r} leaked into the log"


# --- Delivery ---


def test_invite_is_delivered_to_the_invitee_with_the_link(mailgun):
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is True
    sent = mailgun.last
    assert sent["url"] == "https://api.mailgun.net/v3/mg.gghightech.example/messages"
    assert sent["auth"] == ("api", API_KEY)
    assert sent["data"]["to"] == "jane@client.example"
    assert sent["data"]["from"] == "GG HighTech <hello@gghightech.example>"
    assert INVITE_LINK in sent["data"]["text"]
    assert "Jane Doe" in sent["data"]["text"]
    assert sent["timeout"] > 0


def test_link_tracking_is_switched_off(mailgun):
    """Otherwise Mailgun rewrites and records the credential-bearing link."""
    send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK)
    assert mailgun.last["data"]["o:tracking"] == "no"


def test_payment_link_is_delivered_to_the_customer(mailgun):
    assert send_payment_link_email("billing@client.example", CHECKOUT_URL, "October retainer") is True
    assert mailgun.last["data"]["to"] == "billing@client.example"
    assert CHECKOUT_URL in mailgun.last["data"]["text"]
    assert "October retainer" in mailgun.last["data"]["subject"]


def test_lead_notification_goes_to_the_team_inbox_with_the_details(mailgun):
    estimate_id = str(uuid.uuid4())
    assert send_lead_notification(estimate_id, f"{LEAD_EMAIL} / {LEAD_PHONE}", BRIEF) is True
    data = mailgun.last["data"]
    assert data["to"] == "leads@gghightech.example"
    assert LEAD_EMAIL in data["text"] and LEAD_PHONE in data["text"] and BRIEF in data["text"]
    assert estimate_id in data["text"]
    assert LEAD_EMAIL not in data["subject"]


def test_default_sender_and_eu_region(configure, outbox):
    configure(
        ENVIRONMENT="production",
        MAILGUN_API_KEY=API_KEY,
        MAILGUN_DOMAIN="mg.gghightech.example",
        MAILGUN_API_BASE="https://api.eu.mailgun.net/",
        MAILGUN_FROM_EMAIL="",
    )
    send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK)
    assert outbox.last["url"] == "https://api.eu.mailgun.net/v3/mg.gghightech.example/messages"
    assert outbox.last["data"]["from"] == "GG HighTech <no-reply@mg.gghightech.example>"


def test_subject_is_kept_to_one_line(mailgun):
    send_payment_link_email("billing@client.example", CHECKOUT_URL, "Retainer\r\nBcc: attacker@evil.example")
    subject = mailgun.last["data"]["subject"]
    assert "\n" not in subject and "\r" not in subject


# --- Nothing sensitive in the log: success ---


def test_sent_invite_logs_no_link_and_no_full_address(mailgun, logs):
    send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK)
    assert_clean(logs, INVITE_LINK, "INVITE-SECRET", "token=", "jane@client.example", "Jane Doe", API_KEY)
    assert "invite email sent to j***@client.example" in logs.text


def test_sent_payment_link_logs_no_url(mailgun, logs):
    send_payment_link_email("billing@client.example", CHECKOUT_URL, "October retainer")
    assert_clean(logs, CHECKOUT_URL, "PAYMENT-SECRET", "checkout.stripe.com", "billing@client.example", API_KEY)
    assert "payment-link email sent to b***@client.example" in logs.text


def test_sent_lead_notification_logs_no_lead_details(mailgun, logs):
    send_lead_notification(str(uuid.uuid4()), f"{LEAD_EMAIL} / {LEAD_PHONE}", BRIEF)
    assert_clean(logs, LEAD_EMAIL, LEAD_PHONE, BRIEF, "customer portal", API_KEY)


# --- Nothing sensitive in the log: failure ---


def test_transport_failure_returns_false_and_logs_nothing_sensitive(mailgun, logs):
    mailgun.raises = httpx.ConnectError(f"cannot reach https://api:{API_KEY}@api.mailgun.net for {INVITE_LINK}")
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is False
    assert_clean(logs, INVITE_LINK, "INVITE-SECRET", "jane@client.example", API_KEY)
    assert "invite email to j***@client.example failed: ConnectError" in logs.text


def test_unexpected_error_never_propagates(mailgun):
    mailgun.raises = RuntimeError("boom")
    assert send_payment_link_email("billing@client.example", CHECKOUT_URL, "x") is False


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_rejection_by_mailgun_returns_false_and_logs_only_the_status(mailgun, logs, status):
    mailgun.status_code = status
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is False
    assert_clean(logs, INVITE_LINK, "jane@client.example", API_KEY)
    assert f"HTTP {status}" in logs.text


# --- Email not configured ---


def test_unconfigured_in_production_sends_nothing_and_logs_no_link(configure, outbox, logs):
    configure(ENVIRONMENT="production", MAILGUN_API_KEY="", MAILGUN_DOMAIN="")
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is False
    assert send_payment_link_email("billing@client.example", CHECKOUT_URL, "October retainer") is False
    assert send_lead_notification(str(uuid.uuid4()), f"{LEAD_EMAIL} / {LEAD_PHONE}", BRIEF) is False

    assert outbox.sent == []
    assert_clean(logs, INVITE_LINK, "token=", CHECKOUT_URL, LEAD_EMAIL, LEAD_PHONE, BRIEF, "jane@client.example")
    assert "was NOT sent" in logs.text
    assert all(record.levelno >= logging.WARNING for record in logs.records)


@pytest.mark.parametrize("environment", ["staging", "prod", "test"])
def test_only_exact_development_gets_the_console_fallback(configure, outbox, logs, environment):
    configure(ENVIRONMENT=environment, MAILGUN_API_KEY="", MAILGUN_DOMAIN="")
    send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK)
    assert_clean(logs, INVITE_LINK)


def test_partial_configuration_counts_as_unconfigured(configure, outbox, logs):
    configure(ENVIRONMENT="production", MAILGUN_API_KEY=API_KEY, MAILGUN_DOMAIN="")
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is False
    assert outbox.sent == []
    assert_clean(logs, INVITE_LINK, API_KEY)


def test_unconfigured_in_development_prints_the_email_so_local_work_is_possible(configure, outbox, logs):
    configure(ENVIRONMENT="development", MAILGUN_API_KEY="", MAILGUN_DOMAIN="")
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is False
    assert outbox.sent == []
    assert INVITE_LINK in logs.text
    assert "DEV ONLY" in logs.text


def test_configured_in_development_sends_and_does_not_print_the_link(configure, outbox, logs):
    configure(ENVIRONMENT="development", MAILGUN_API_KEY=API_KEY, MAILGUN_DOMAIN="mg.gghightech.example")
    assert send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK) is True
    assert_clean(logs, INVITE_LINK)


# --- Masking ---


@pytest.mark.parametrize(
    "address, masked",
    [
        ("jane@client.example", "j***@client.example"),
        ("a@b.co", "a***@b.co"),
        ("not-an-email", "***"),
        ("@nolocal.example", "***"),
        ("", "***"),
        (None, "***"),
    ],
)
def test_mask_email(address, masked):
    assert mask_email(address) == masked


# --- The old stub's trap is gone ---


def test_setting_a_mail_key_no_longer_raises_not_implemented(mailgun):
    assert not hasattr(settings, "RESEND_API_KEY")
    for send in (
        lambda: send_invite_email("jane@client.example", "Jane Doe", INVITE_LINK),
        lambda: send_payment_link_email("billing@client.example", CHECKOUT_URL, "x"),
        lambda: send_lead_notification("abc", LEAD_EMAIL, BRIEF),
    ):
        assert send() is True


# --- Through the real routes ---


@pytest.fixture
def db():
    fake = FakeSession()
    fake.client_org = Organization(id=uuid.uuid4(), name="Acme Ltd", domain="acme.example", plan_tier="STANDARD")
    fake._objects[(Organization, fake.client_org.id)] = fake.client_org
    real_app.dependency_overrides[get_db] = lambda: fake
    return fake


@pytest.fixture
def client(db):
    return TestClient(real_app)


def invite_body(db):
    return {"org_id": str(db.client_org.id), "email": "jane@client.example", "full_name": "Jane Doe", "role": "CLIENT_ADMIN"}


def test_inviting_a_user_emails_the_link_and_keeps_it_out_of_the_log(client, db, mailgun, logs):
    response = client.post("/api/v1/users/invite", json=invite_body(db), headers=bearer(token_for("SUPER_ADMIN")))

    assert response.status_code == 201
    (sent,) = mailgun.sent
    assert sent["data"]["to"] == "jane@client.example"
    assert "/accept-invite?token=" in sent["data"]["text"]
    assert_clean(logs, "accept-invite?token=", "jane@client.example")
    assert "token" not in response.json()  # nor is the link handed back to the caller


def test_invite_still_succeeds_when_mail_delivery_fails(client, db, mailgun):
    mailgun.status_code = 500
    response = client.post("/api/v1/users/invite", json=invite_body(db), headers=bearer(token_for("SUPER_ADMIN")))
    assert response.status_code == 201


def test_invite_in_production_without_mail_logs_no_link(client, db, configure, outbox, logs):
    configure(ENVIRONMENT="production", MAILGUN_API_KEY="", MAILGUN_DOMAIN="")
    response = client.post("/api/v1/users/invite", json=invite_body(db), headers=bearer(token_for("SUPER_ADMIN")))
    assert response.status_code == 201
    assert_clean(logs, "accept-invite?token=", "jane@client.example")


ESTIMATE_BODY = {
    "project_type": "WEB_APPLICATION",
    "features": ["AUTH"],
    "design_tier": "STANDARD",
    "request_type": "NEW",
    "client_email": LEAD_EMAIL,
    "client_phone": LEAD_PHONE,
    "project_description": BRIEF,
}


def test_submitting_an_estimate_notifies_the_team_and_logs_no_lead_details(client, mailgun, logs):
    response = client.post("/api/v1/estimates", json=ESTIMATE_BODY)

    assert response.status_code == 201
    (sent,) = mailgun.sent
    assert sent["data"]["to"] == "leads@gghightech.example"
    assert LEAD_EMAIL in sent["data"]["text"] and BRIEF in sent["data"]["text"]
    assert_clean(logs, LEAD_EMAIL, LEAD_PHONE, BRIEF)


def test_submitting_an_estimate_in_production_without_mail_logs_no_lead_details(client, configure, outbox, logs):
    configure(ENVIRONMENT="production", MAILGUN_API_KEY="", MAILGUN_DOMAIN="")
    assert client.post("/api/v1/estimates", json=ESTIMATE_BODY).status_code == 201
    assert_clean(logs, LEAD_EMAIL, LEAD_PHONE, BRIEF)


def test_anonymous_estimate_without_contact_details_sends_no_email(client, mailgun):
    body = {k: v for k, v in ESTIMATE_BODY.items() if k not in ("client_email", "client_phone")}
    assert client.post("/api/v1/estimates", json=body).status_code == 201
    assert mailgun.sent == []
