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

    # Frontend origin(s) allowed to call this API.
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Clerk (auth). Left blank until the Clerk account/keys are provisioned —
    # see app/services/auth.py for the dev-mode fallback this enables.
    CLERK_SECRET_KEY: str = ""
    CLERK_JWKS_URL: str = ""
    CLERK_ISSUER: str = ""

    # Self-issued staff login (see app/services/local_auth.py) — real
    # bcrypt password auth used until Clerk replaces it. Blank disables
    # local login entirely (POST /auth/login returns 503) rather than
    # ever falling back to a hardcoded default signing key. Generate one
    # with `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
    SECRET_KEY: str = ""
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24

    # Email (GGH-202 lead notifications). Stubbed for Phase 0 — see
    # app/services/email.py. Fill in once a Resend (or similar) account exists.
    RESEND_API_KEY: str = ""
    LEAD_NOTIFICATION_EMAIL: str = "leads@gghightech.example"


settings = Settings()
