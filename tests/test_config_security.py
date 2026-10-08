"""SEC-01 — startup configuration must fail closed.

These build fresh Settings objects from environment variables only
(_env_file=None), so a developer's local .env can't influence the result.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import MIN_SECRET_KEY_LENGTH, Settings, insecure_secret_key_reason

BACKEND_ROOT = Path(__file__).resolve().parents[1]
STRONG_KEY = secrets.token_urlsafe(48)
SECURITY_VARS = ("ENVIRONMENT", "ALLOW_DEV_AUTH_HEADERS", "SECRET_KEY")


@pytest.fixture
def build_settings(monkeypatch):
    """Settings as the process would see them with exactly the given
    security-related environment variables and nothing else."""

    def _build(**env):
        for name in SECURITY_VARS:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    return _build


# --- E. Missing ENVIRONMENT must not enable development behaviour ---


def test_missing_environment_defaults_to_production(build_settings):
    s = build_settings(SECRET_KEY=STRONG_KEY)
    assert s.ENVIRONMENT == "production"
    assert s.is_development is False
    assert s.dev_auth_headers_enabled is False


def test_blank_environment_is_treated_as_production(build_settings):
    s = build_settings(ENVIRONMENT="   ", SECRET_KEY=STRONG_KEY)
    assert s.ENVIRONMENT == "production"
    assert s.dev_auth_headers_enabled is False


def test_missing_environment_with_dev_auth_flag_refuses_to_start(build_settings):
    with pytest.raises(ValidationError, match="ALLOW_DEV_AUTH_HEADERS"):
        build_settings(ALLOW_DEV_AUTH_HEADERS="true", SECRET_KEY=STRONG_KEY)


def test_missing_environment_without_secret_key_refuses_to_start(build_settings):
    with pytest.raises(ValidationError, match="SECRET_KEY is not set"):
        build_settings()


# --- Production requires a strong SECRET_KEY ---


@pytest.mark.parametrize("environment", ["production", "Production", "staging", "prod", "test"])
def test_non_development_environments_require_secret_key(build_settings, environment):
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        build_settings(ENVIRONMENT=environment)


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "   ",
        "short",
        "a" * (MIN_SECRET_KEY_LENGTH - 1),
        "a" * 64,
        "CHANGE_ME_CHANGE_ME_CHANGE_ME_CHANGE_ME_1234567890",
        "changeme-please-this-is-a-long-enough-value-0987654321",
        "my-super-secret-key-for-the-gghightech-api-2026",
        "your-secret-here-0123456789abcdefghijklmnopqrstuv",
    ],
)
def test_production_rejects_insecure_secret_key(build_settings, bad_key):
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        build_settings(ENVIRONMENT="production", SECRET_KEY=bad_key)


def test_production_accepts_generated_secret_key(build_settings):
    s = build_settings(ENVIRONMENT="production", SECRET_KEY=STRONG_KEY)
    assert s.SECRET_KEY == STRONG_KEY
    assert insecure_secret_key_reason(s.SECRET_KEY) == ""


@pytest.mark.parametrize("environment", ["production", "staging", "prod"])
def test_dev_auth_flag_outside_development_refuses_to_start(build_settings, environment):
    with pytest.raises(ValidationError, match="ALLOW_DEV_AUTH_HEADERS"):
        build_settings(ENVIRONMENT=environment, ALLOW_DEV_AUTH_HEADERS="true", SECRET_KEY=STRONG_KEY)


# --- Development: relaxed startup, but dev auth still needs the opt-in ---


def test_development_starts_without_secret_key_and_dev_auth_stays_off(build_settings):
    s = build_settings(ENVIRONMENT="development")
    assert s.is_development is True
    assert s.dev_auth_headers_enabled is False


def test_development_with_explicit_false_keeps_dev_auth_off(build_settings):
    s = build_settings(ENVIRONMENT="development", ALLOW_DEV_AUTH_HEADERS="false")
    assert s.dev_auth_headers_enabled is False


@pytest.mark.parametrize("environment", ["development", "Development", " DEVELOPMENT "])
def test_development_with_explicit_opt_in_enables_dev_auth(build_settings, environment):
    s = build_settings(ENVIRONMENT=environment, ALLOW_DEV_AUTH_HEADERS="true")
    assert s.ENVIRONMENT == "development"
    assert s.dev_auth_headers_enabled is True


@pytest.mark.parametrize("near_miss", ["dev", "develop", "development1", "local", "debug"])
def test_only_the_exact_word_development_counts(build_settings, near_miss):
    s = build_settings(ENVIRONMENT=near_miss, SECRET_KEY=STRONG_KEY)
    assert s.is_development is False
    assert s.dev_auth_headers_enabled is False


# --- The real process: importing the app is what "starting" means ---


def _import_app(tmp_path, **env):
    """Import app.main in a fresh interpreter, from a directory with no
    .env file, with only the given security variables set."""
    child_env = {k: v for k, v in os.environ.items() if k not in SECURITY_VARS}
    child_env.update(env)
    child_env["PYTHONPATH"] = str(BACKEND_ROOT)
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_app_refuses_to_start_without_environment_or_secret_key(tmp_path):
    result = _import_app(tmp_path)
    assert result.returncode != 0
    assert "SECRET_KEY is not set" in result.stderr


def test_app_refuses_to_start_in_production_with_dev_auth_flag(tmp_path):
    result = _import_app(tmp_path, ENVIRONMENT="production", SECRET_KEY=STRONG_KEY, ALLOW_DEV_AUTH_HEADERS="true")
    assert result.returncode != 0
    assert "ALLOW_DEV_AUTH_HEADERS" in result.stderr


def test_app_starts_in_production_with_strong_secret_key(tmp_path):
    result = _import_app(tmp_path, ENVIRONMENT="production", SECRET_KEY=STRONG_KEY)
    assert result.returncode == 0, result.stderr
