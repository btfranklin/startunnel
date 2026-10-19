"""Production settings fail closed before a service starts."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.production"
    environment.pop("STARTUNNEL_SECURE_SSL_REDIRECT", None)
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "STARTUNNEL_SECRET_KEY": "test-production-secret-key-" + "x" * 64,
            "STARTUNNEL_API_KEY_PEPPER": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
            "STARTUNNEL_IDEMPOTENCY_SECRET": "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ",
            "STARTUNNEL_METRICS_TOKEN": "BgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgY",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "STARTUNNEL_ALLOWED_HOSTS": "test.startunnel.test",
            "STARTUNNEL_CSRF_TRUSTED_ORIGINS": "",
            "STARTUNNEL_BASE_URL": "https://test.startunnel.test",
        }
    )
    return environment


def _import_production(
    environment: dict[str, str], code: str = "import startunnel.settings.production"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("name", ["STARTUNNEL_SECURE_SSL_REDIRECT"])
def test_production_rejects_invalid_boolean_settings(name: str) -> None:
    environment = _environment()
    environment[name] = "tru"
    result = _import_production(environment)
    assert result.returncode != 0
    assert f"{name} must be true or false." in result.stderr


@pytest.mark.parametrize("value", ["true", "false"])
def test_production_accepts_explicit_true_and_false_boolean_settings(value: str) -> None:
    environment = _environment()
    environment["STARTUNNEL_SECURE_SSL_REDIRECT"] = value
    result = _import_production(environment)
    assert result.returncode == 0, result.stderr


def test_metrics_token_must_be_distinct_from_cryptographic_secrets() -> None:
    environment = _environment()
    environment["STARTUNNEL_METRICS_TOKEN"] = environment["STARTUNNEL_API_KEY_PEPPER"]
    result = _import_production(environment)
    assert result.returncode != 0
    assert "Each StarTunnel cryptographic secret must be different." in result.stderr


def test_production_preserves_printable_text_secrets_without_decoding() -> None:
    environment = _environment()
    values = {
        "STARTUNNEL_API_KEY_PEPPER": "ab" * 32,
        "STARTUNNEL_IDEMPOTENCY_SECRET": base64.urlsafe_b64encode(b"x" * 64)
        .decode("ascii")
        .rstrip("="),
        "STARTUNNEL_METRICS_TOKEN": "operator token / with spaces and punctuation!",
    }
    environment.update(values)
    code = (
        "from startunnel.settings import production as p; "
        "assert p.API_KEY_PEPPER == "
        + repr(values["STARTUNNEL_API_KEY_PEPPER"])
        + "; assert p.IDEMPOTENCY_SECRET == "
        + repr(values["STARTUNNEL_IDEMPOTENCY_SECRET"])
        + "; assert p.METRICS_TOKEN == "
        + repr(values["STARTUNNEL_METRICS_TOKEN"])
    )
    result = _import_production(environment, code)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("x" * 31, "at least 32 characters"),
        ("x" * 31 + "é", "printable single-line ASCII"),
        ("x" * 31 + "\x7f", "printable single-line ASCII"),
        ("x" * 31 + "\t", "printable single-line ASCII"),
    ],
)
def test_production_rejects_short_or_nonprintable_secret_values(value: str, message: str) -> None:
    environment = _environment()
    environment["STARTUNNEL_API_KEY_PEPPER"] = value
    result = _import_production(environment)
    assert result.returncode != 0
    assert message in result.stderr


@pytest.mark.parametrize(
    "database_url",
    ["sqlite:////tmp/startunnel-test.sqlite3", "mysql://test:test@localhost/test"],
)
def test_production_requires_postgresql(database_url: str) -> None:
    environment = _environment()
    environment["DATABASE_URL"] = database_url
    result = _import_production(environment)
    assert result.returncode != 0
    assert "DATABASE_URL must use PostgreSQL" in result.stderr


def test_production_keeps_the_configured_postgresql_pool() -> None:
    environment = _environment()
    environment["STARTUNNEL_DATABASE_POOL_MAX_SIZE"] = "18"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from startunnel.settings.production import DATABASES; "
            "database = DATABASES['default']; "
            "assert database['CONN_MAX_AGE'] == 0; "
            "assert database['OPTIONS']['pool'] == "
            "{'min_size': 1, 'max_size': 18, 'max_waiting': 256, 'timeout': 5}",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "base_url",
    [
        "https:///missing-host",
        "https://person:password@test.startunnel.test",
        "https://test.startunnel.test/not-an-origin",
        "https://test.startunnel.test?query=1",
        "https://test.startunnel.test#fragment",
    ],
)
def test_production_requires_one_allowed_public_origin(base_url: str) -> None:
    environment = _environment()
    environment["STARTUNNEL_BASE_URL"] = base_url
    result = _import_production(environment)
    assert result.returncode != 0
    assert "STARTUNNEL_BASE_URL" in result.stderr


@pytest.mark.parametrize(
    ("base_url", "canonical_url"),
    [
        ("http://web:8000", "http://web:8000"),
        ("http://192.168.1.20:8000", "http://192.168.1.20:8000"),
        ("http://10.8.0.5", "http://10.8.0.5"),
        ("http://10.8.0.5:8080/", "http://10.8.0.5:8080"),
    ],
)
def test_production_http_origin_disables_https_only_settings(
    base_url: str, canonical_url: str
) -> None:
    environment = _environment()
    environment["STARTUNNEL_ALLOWED_HOSTS"] = base_url.split("//", 1)[1].split(":", 1)[0]
    environment["STARTUNNEL_BASE_URL"] = base_url
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from startunnel.settings.production import *; "
            "from django.conf import settings; "
            "assert STARTUNNEL_BASE_URL == " + repr(canonical_url) + "; "
            "assert SECURE_SSL_REDIRECT is False; "
            "assert SESSION_COOKIE_SECURE is False; "
            "assert CSRF_COOKIE_SECURE is False; "
            "assert settings.SECURE_HSTS_SECONDS == 0; "
            "assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is False; "
            "assert settings.SECURE_HSTS_PRELOAD is False",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_production_https_defaults_to_secure_settings() -> None:
    environment = _environment()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from startunnel.settings.production import *; "
            "from django.conf import settings; "
            "assert SECURE_SSL_REDIRECT is True; "
            "assert SESSION_COOKIE_SECURE is True; "
            "assert CSRF_COOKIE_SECURE is True; "
            "assert settings.SECURE_HSTS_SECONDS == 0; "
            "assert not settings.SECURE_HSTS_INCLUDE_SUBDOMAINS; "
            "assert not settings.SECURE_HSTS_PRELOAD",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_production_allows_django_host_wildcards() -> None:
    environment = _environment()
    environment["STARTUNNEL_ALLOWED_HOSTS"] = ".example.test"
    environment["STARTUNNEL_BASE_URL"] = "https://outside.example"
    code = """
import django
django.setup()
from django.core.exceptions import DisallowedHost
from django.test import RequestFactory
from unittest import TestCase
factory = RequestFactory()
assert factory.get("/", HTTP_HOST="example.test").get_host() == "example.test"
assert factory.get("/", HTTP_HOST="api.example.test").get_host() == "api.example.test"
request = factory.get("/", HTTP_HOST="outside.example")
TestCase().assertRaises(DisallowedHost, request.get_host)
"""
    result = _import_production(environment, code)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("trusted_origins", "origin", "accepted"),
    [
        ("", "https://app.example.test", True),
        ("https://*.trusted.example", "https://api.trusted.example", True),
        ("https://*.trusted.example", "https://untrusted.example", False),
    ],
)
def test_production_csrf_uses_django_origin_validation(
    trusted_origins: str, origin: str, accepted: bool
) -> None:
    environment = _environment()
    environment["STARTUNNEL_ALLOWED_HOSTS"] = ".example.test"
    environment["STARTUNNEL_CSRF_TRUSTED_ORIGINS"] = trusted_origins
    result = _import_production(
        environment,
        "import django; django.setup(); "
        "from django.conf import settings; "
        "from django.http import HttpResponse; "
        "from django.middleware.csrf import CsrfViewMiddleware, get_token; "
        "from django.test import RequestFactory; "
        "request = RequestFactory().post('/', secure=True, HTTP_HOST='app.example.test', "
        + "HTTP_ORIGIN="
        + repr(origin)
        + "); request._dont_enforce_csrf_checks = False; "
        "token = get_token(request); "
        "request.COOKIES[settings.CSRF_COOKIE_NAME] = request.META['CSRF_COOKIE']; "
        "request.META['HTTP_X_CSRFTOKEN'] = token; "
        "response = CsrfViewMiddleware(lambda request: HttpResponse('ok')).process_view("
        "request, lambda request: HttpResponse('ok'), (), {}); "
        "assert (response is None) is " + repr(accepted),
    )
    assert result.returncode == 0, result.stderr


def test_production_canonicalizes_public_origin_trailing_slash() -> None:
    environment = _environment()
    environment["STARTUNNEL_BASE_URL"] = "https://test.startunnel.test/"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from startunnel.settings.production import STARTUNNEL_BASE_URL; "
            "assert STARTUNNEL_BASE_URL == 'https://test.startunnel.test'",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
