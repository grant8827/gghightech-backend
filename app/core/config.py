"""
Application configuration.

Values are read from environment variables (and a local .env file in dev,
see .env.example). Nothing here should hold real secrets — those live only
in .env, which is gitignored.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "GG HighTech API"
    ENVIRONMENT: str = "development"

    # Local Postgres by default — see gghightech_dev created for Phase 0.
    DATABASE_URL: str = "postgresql+psycopg2://localhost/gghightech_dev"

    # Frontend origin(s) allowed to call this API (CORS).
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # The canonical frontend origin, for building links that get emailed out
    # (e.g. the invite-accept link in app/api/routes/users.py). Distinct from
    # CORS_ORIGINS, which is "who may call this API," not "where the app is."
    FRONTEND_URL: str = "http://localhost:3000"

    # Self-issued email/password login (see app/services/local_auth.py) —
    # the site's only auth system. Blank disables local login entirely
    # (POST /auth/login returns 503) rather than ever falling back to a
    # hardcoded default signing key. Generate one with
    # `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # One-time bootstrap for the very first SUPER_ADMIN in an environment
    # with no shell/DB access available (see app/api/routes/auth.py). Blank
    # disables the endpoint entirely (404) — set it, call the endpoint
    # once, then unset it. Never leave this set.
    BOOTSTRAP_TOKEN: str = ""

    # Email (GGH-202 lead notifications). Stubbed for Phase 0 — see
    # app/services/email.py. Fill in once a Resend (or similar) account exists.
    RESEND_API_KEY: str = ""
    LEAD_NOTIFICATION_EMAIL: str = "leads@gghightech.example"

    # Billing (invoice payment). Stubbed — see app/services/stripe_service.py.
    # Fill in once a Stripe account exists.
    STRIPE_SECRET_KEY: str = ""

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


settings = Settings()
