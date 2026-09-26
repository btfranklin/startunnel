"""Shared Django settings for StarTunnel."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import dj_database_url
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = PROJECT_ROOT / "src"
OPENAI_ENV_NAMES = frozenset({"OPENAI_API_KEY", "OPENAI_MODEL"})


def _load_local_environment(path: Path) -> None:
    """Load local StarTunnel settings without giving OpenAI keys to Django."""

    for dotenv_name, dotenv_value in dotenv_values(path).items():
        if dotenv_name in OPENAI_ENV_NAMES or dotenv_value is None:
            continue
        os.environ.setdefault(dotenv_name, dotenv_value)


_load_local_environment(PROJECT_ROOT / ".env")


def env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"{name} must be set.")
    return value or ""


def env_bool(name: str, default: bool) -> bool:
    """Read one explicit true or false setting and reject spelling errors."""

    value = env(name, "true" if default else "false").strip().lower()
    if value not in {"true", "false"}:
        raise RuntimeError(f"{name} must be true or false.")
    return value == "true"


STARTUNNEL_ENV = env("STARTUNNEL_ENV", "development")
SECRET_KEY = env("STARTUNNEL_SECRET_KEY", "unsafe-development-key")
DEBUG = False
ALLOWED_HOSTS = [
    host.strip() for host in env("STARTUNNEL_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in env("STARTUNNEL_CSRF_TRUSTED_ORIGINS", "").split(",") if origin
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.postgres",
    "django.contrib.staticfiles",
    "django_htmx",
    "core.apps.CoreConfig",
    "accounts.apps.AccountsConfig",
    "agents.apps.AgentsConfig",
    "tunnels.apps.TunnelsConfig",
    "api.apps.ApiConfig",
    "site_app.apps.SiteAppConfig",
]

MIDDLEWARE = [
    "core.middleware.TrustedProxyMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.RequestIdMiddleware",
    "core.middleware.SecurityHeadersMiddleware",
    "api.middleware.ApiErrorEnvelopeMiddleware",
    "core.middleware.DatabaseMetricsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "startunnel.urls"
WSGI_APPLICATION = "startunnel.wsgi.application"
ASGI_APPLICATION = "startunnel.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [PROJECT_ROOT / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]

DATABASE_POOL_MAX_SIZE = int(env("STARTUNNEL_DATABASE_POOL_MAX_SIZE", "0"))
if not 0 <= DATABASE_POOL_MAX_SIZE <= 40:
    raise RuntimeError("STARTUNNEL_DATABASE_POOL_MAX_SIZE must be from 0 through 40.")


def database_configuration(url: str) -> dict[str, Any]:
    """Build one database configuration and keep its bounded pool policy."""

    configuration = dj_database_url.parse(
        url,
        conn_max_age=60,
        conn_health_checks=True,
    )
    if DATABASE_POOL_MAX_SIZE and configuration["ENGINE"] == "django.db.backends.postgresql":
        configuration["CONN_MAX_AGE"] = 0
        configuration.setdefault("OPTIONS", {})["pool"] = {
            "min_size": 1,
            "max_size": DATABASE_POOL_MAX_SIZE,
            "max_waiting": 256,
            "timeout": 5,
        }
    return dict(configuration)


DATABASES: dict[str, Any] = {
    "default": database_configuration(
        env("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'db.sqlite3'}")
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = PROJECT_ROOT / "staticfiles"
STATICFILES_DIRS = [PROJECT_ROOT / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"},
}

LOGIN_REDIRECT_URL = "/app/"
LOGOUT_REDIRECT_URL = "/"
LOGIN_URL = "/accounts/login/"

API_KEY_PEPPER = env("STARTUNNEL_API_KEY_PEPPER", "development-api-pepper")
STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = int(env("STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT", "50"))
if not 1 <= STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT <= 10_000:
    raise RuntimeError("STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT must be from 1 through 10000.")
ADDRESS_SECRET = env("STARTUNNEL_ADDRESS_SECRET", "development-address-secret")
ADDRESS_DERIVATION_SECRET = env(
    "STARTUNNEL_ADDRESS_DERIVATION_SECRET", "development-address-derivation-secret"
)
IDEMPOTENCY_SECRET = env("STARTUNNEL_IDEMPOTENCY_SECRET", "development-idempotency-secret")
METRICS_TOKEN = env("STARTUNNEL_METRICS_TOKEN", "development-metrics-token")
STARTUNNEL_BASE_URL = env("STARTUNNEL_BASE_URL", "http://localhost:8000")
STARTUNNEL_TRUSTED_PROXY_CIDRS = [
    network.strip()
    for network in env("STARTUNNEL_TRUSTED_PROXY_CIDRS", "127.0.0.0/8").split(",")
    if network.strip()
]
STARTUNNEL_MAX_REQUEST_BODY_BYTES = int(env("STARTUNNEL_MAX_REQUEST_BODY_BYTES", "131072"))
if not 65_536 <= STARTUNNEL_MAX_REQUEST_BODY_BYTES <= 1_048_576:
    raise RuntimeError("STARTUNNEL_MAX_REQUEST_BODY_BYTES must be from 65536 through 1048576.")
DATA_UPLOAD_MAX_MEMORY_SIZE = STARTUNNEL_MAX_REQUEST_BODY_BYTES

_history_retention = env("STARTUNNEL_HISTORY_RETENTION_SECONDS", str(30 * 86_400)).strip()
if _history_retention == "forever":
    STARTUNNEL_HISTORY_RETENTION_SECONDS: int | None = None
else:
    try:
        STARTUNNEL_HISTORY_RETENTION_SECONDS = int(_history_retention)
    except ValueError as error:
        raise RuntimeError(
            "STARTUNNEL_HISTORY_RETENTION_SECONDS must be a positive integer or forever."
        ) from error
    if STARTUNNEL_HISTORY_RETENTION_SECONDS <= 0:
        raise RuntimeError(
            "STARTUNNEL_HISTORY_RETENTION_SECONDS must be a positive integer or forever."
        )

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"

LOG_LEVEL = env("STARTUNNEL_LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"request_id": {"()": "core.logging.RequestIdFilter"}},
    "formatters": {
        "json": {
            "()": "core.logging.RedactingJsonFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["request_id"],
            "formatter": "json",
        }
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.server": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}
