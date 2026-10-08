"""
Application configuration.

Values are read from environment variables (and a local .env file in dev,
see .env.example). Nothing here should hold real secrets — those live only
in .env, which is gitignored.
"""

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT = "development"
PRODUCTION = "production"

# A signing key shorter than this is rejected outside development. The
# generator in .env.example (secrets.token_urlsafe(48)) produces 64 chars.
MIN_SECRET_KEY_LENGTH = 32

# Substrings that mark a SECRET_KEY as a copied placeholder rather than a
# generated value. Compared case-insensitively.
_PLACEHOLDER_SECRET_MARKERS = (
    "changeme",
    "change_me",
    "change-me",
    "replace_me",
    "replace-me",
    "your-secret",
    "your_secret",
    "secret_key",
    "secret-key",
    "example",
    "placeholder",
    "password",
)


def insecure_secret_key_reason(value: str) -> str:
    """Why `value` is not acceptable as a production signing key, or "" if
    it is. Kept as a plain function so tests can exercise it directly."""
    if not value:
        return "SECRET_KEY is not set"
    if len(value) < MIN_SECRET_KEY_LENGTH:
        return f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters"
    lowered = value.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_SECRET_MARKERS):
        return "SECRET_KEY looks like a placeholder, not a generated value"
    if len(set(value)) < 10:
        return "SECRET_KEY has too little variety to be a generated value"
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "GG HighTech API"

    # Fails closed: an omitted or blank ENVIRONMENT means production. Only
    # the exact value "development" relaxes anything, and even then the
    # header-based dev auth below stays off unless separately opted into.
    ENVIRONMENT: str = PRODUCTION

    # Explicit opt-in for the X-Dev-User-Role / X-Dev-User-Email auth
    # shortcut (see app/services/auth.py). Only honoured when ENVIRONMENT
    # is exactly "development"; setting it anywhere else refuses to start.
    ALLOW_DEV_AUTH_HEADERS: bool = False

    # Local Postgres by default — see gghightech_dev created for Phase 0.
    DATABASE_URL: str = "postgresql+psycopg2://localhost/gghightech_dev"

    # Frontend origin(s) allowed to call this API (CORS).
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # The canonical frontend origin, for building links that get emailed out
    # (e.g. the invite-accept link in app/api/routes/users.py). Distinct from
    # CORS_ORIGINS, which is "who may call this API," not "where the app is."
    FRONTEND_URL: str = "http://localhost:3000"

    # Self-issued email/password login (see app/services/local_auth.py) —
    # the site's only auth system. Required outside development: the app
    # refuses to start without a strong value (see _validate_security).
    # In development, blank disables local login entirely (POST /auth/login
    # returns 503) rather than ever falling back to a hardcoded default
    # signing key. Generate one with
    # `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # One-time bootstrap for the very first SUPER_ADMIN in an environment
    # with no shell/DB access available (see app/api/routes/auth.py). Blank
    # disables the endpoint entirely (404) — set it, call the endpoint
    # once, then unset it. Never leave this set.
    BOOTSTRAP_TOKEN: str = ""

    # Transactional email via Mailgun (app/services/email.py): invite links,
    # payment links, and new-lead notifications. Sending is on only when
    # both MAILGUN_API_KEY and MAILGUN_DOMAIN are set. MAILGUN_API_BASE is
    # https://api.eu.mailgun.net for EU-region domains. MAILGUN_FROM_EMAIL
    # defaults to "GG HighTech <no-reply@MAILGUN_DOMAIN>".
    MAILGUN_API_KEY: str = ""
    MAILGUN_DOMAIN: str = ""
    MAILGUN_API_BASE: str = "https://api.mailgun.net"
    MAILGUN_FROM_EMAIL: str = ""
    # Where new-lead notifications are sent.
    LEAD_NOTIFICATION_EMAIL: str = "leads@gghightech.example"

    # Billing (invoice payment). Stubbed — see app/services/stripe_service.py.
    # Fill in once a Stripe account exists.
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_SUCCESS_URL: str = "http://localhost:3000/portal?payment=success"
    STRIPE_CANCEL_URL: str = "http://localhost:3000/portal?payment=cancelled"
    STRIPE_PORTAL_RETURN_URL: str = "http://localhost:3000/portal"

    # Optional AI-assisted estimate analysis. When blank, the estimator uses
    # the deterministic scope heuristics in app/services/scope_analysis.py.
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-5-mini"

    # Abuse protection for the public estimate endpoints — see
    # app/services/rate_limit.py (per client) and app/services/ai_budget.py
    # (global). The per-client limits are per IP address; 0 disables one.
    ESTIMATE_CREATE_LIMIT_PER_10_MIN: int = Field(default=10, ge=0)
    ESTIMATE_CREATE_LIMIT_PER_DAY: int = Field(default=40, ge=0)
    ESTIMATE_PREVIEW_LIMIT_PER_MIN: int = Field(default=60, ge=0)
    # Global, across all visitors. Over either one, estimates fall back to
    # the deterministic pricing rules instead of calling the AI. 0 turns
    # AI analysis off entirely.
    AI_ANALYSIS_DAILY_CAP: int = Field(default=200, ge=0)
    AI_ANALYSIS_MAX_CONCURRENT: int = Field(default=4, ge=0)

    # Brute-force protection for POST /auth/login (app/api/routes/auth.py).
    # 0 disables a limit. Attempts per IP address, successful or not:
    LOGIN_ATTEMPTS_PER_IP_PER_5_MIN: int = Field(default=20, ge=0)
    # Wrong passwords for one email from one IP address before that pair is
    # locked out, and for one email from ANY address before the account is.
    # Both count within, and lock for, LOGIN_LOCKOUT_MINUTES.
    LOGIN_FAILURES_BEFORE_LOCKOUT: int = Field(default=5, ge=0)
    LOGIN_FAILURES_BEFORE_ACCOUNT_LOCKOUT: int = Field(default=20, ge=0)
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1)

    # How many reverse proxies / load balancers this app sits behind, for
    # reading the real client address out of X-Forwarded-For. 0 (default)
    # ignores that header and uses the direct connection's address, which
    # is the only safe choice when the app is reachable directly. Behind a
    # platform proxy, set this to the number of proxies (usually 1) or
    # every visitor will share one rate-limit bucket.
    TRUSTED_PROXY_HOPS: int = Field(default=0, ge=0)

    # GitHub sync (app/services/github_service.py). Optional — reads work
    # unauthenticated against public repos (rate-limited by IP). Set this
    # for private repos or a higher rate limit: a fine-grained PAT with
    # read-only "Contents" access is enough.
    GITHUB_TOKEN: str = ""

    # Jira sync (app/services/jira_service.py). All three required — Jira's
    # API needs auth for every call, unlike GitHub's. JIRA_BASE_URL is your
    # site, e.g. https://your-team.atlassian.net; JIRA_API_TOKEN is created
    # at id.atlassian.com/manage-profile/security/api-tokens.
    JIRA_BASE_URL: str = ""
    JIRA_EMAIL: str = ""
    JIRA_API_TOKEN: str = ""

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def _normalize_environment(cls, value: object) -> object:
        """ "Development", " development " and "development" must not behave
        differently, and a blank value must not behave differently from an
        omitted one."""
        if isinstance(value, str):
            return value.strip().lower() or PRODUCTION
        return value

    @field_validator("SECRET_KEY", mode="before")
    @classmethod
    def _strip_secret_key(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        """Startup validation. `settings = Settings()` below runs at import
        time, so a failure here stops the API, Alembic and the CLI from
        starting at all rather than running in an unsafe state."""
        if self.is_development:
            return self

        if self.ALLOW_DEV_AUTH_HEADERS:
            raise ValueError(
                f"ALLOW_DEV_AUTH_HEADERS is enabled but ENVIRONMENT is {self.ENVIRONMENT!r}. "
                f"Header-based dev auth is only permitted when ENVIRONMENT={DEVELOPMENT}. "
                "Unset ALLOW_DEV_AUTH_HEADERS."
            )

        reason = insecure_secret_key_reason(self.SECRET_KEY)
        if reason:
            raise ValueError(
                f"{reason}. A strong SECRET_KEY is required when ENVIRONMENT is {self.ENVIRONMENT!r} "
                '(generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))").'
            )
        return self

    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT == DEVELOPMENT

    @property
    def dev_auth_headers_enabled(self) -> bool:
        """The single switch app/services/auth.py consults. Both conditions
        are required: an explicit opt-in AND an explicit development
        environment. Neither is a default."""
        return self.is_development and self.ALLOW_DEV_AUTH_HEADERS


settings = Settings()
