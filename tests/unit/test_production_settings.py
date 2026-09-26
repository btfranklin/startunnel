"""Production settings fail closed before a service starts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "src"),
            "STARTUNNEL_SECRET_KEY": "test-production-secret-key-" + "x" * 64,
            "STARTUNNEL_API_KEY_PEPPER": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
            "STARTUNNEL_ADDRESS_SECRET": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
            "STARTUNNEL_ADDRESS_DERIVATION_SECRET": ("AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM"),
            "STARTUNNEL_IDEMPOTENCY_SECRET": "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ",
            "STARTUNNEL_METRICS_TOKEN": "BgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgY",
            "DATABASE_URL": "postgresql://test:test@localhost/test",
            "STARTUNNEL_ALLOWED_HOSTS": "test.startunnel.test",
            "STARTUNNEL_CSRF_TRUSTED_ORIGINS": "https://test.startunnel.test",
            "STARTUNNEL_BASE_URL": "https://test.startunnel.test",
            "STARTUNNEL_SECURE_SSL_REDIRECT": "true",
        }
    )
    return environment


def _import_production(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", "import startunnel.settings.production"],
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


def test_production_accepts_explicit_true_and_false_boolean_settings() -> None:
    environment = _environment()
    environment["STARTUNNEL_SECURE_SSL_REDIRECT"] = "false"
    result = _import_production(environment)
    assert result.returncode == 0, result.stderr


def test_metrics_token_must_be_distinct_from_cryptographic_secrets() -> None:
    environment = _environment()
    environment["STARTUNNEL_METRICS_TOKEN"] = environment["STARTUNNEL_API_KEY_PEPPER"]
    result = _import_production(environment)
    assert result.returncode != 0
    assert "Each StarTunnel cryptographic secret must be different." in result.stderr


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
        "https://other.startunnel.test",
    ],
)
def test_production_requires_one_allowed_public_https_origin(base_url: str) -> None:
    environment = _environment()
    environment["STARTUNNEL_BASE_URL"] = base_url
    result = _import_production(environment)
    assert result.returncode != 0
    assert "STARTUNNEL_BASE_URL" in result.stderr


def test_production_requires_public_origin_in_csrf_trust() -> None:
    environment = _environment()
    environment["STARTUNNEL_CSRF_TRUSTED_ORIGINS"] = "https://other.startunnel.test"
    result = _import_production(environment)
    assert result.returncode != 0
    assert "origin must be in STARTUNNEL_CSRF_TRUSTED_ORIGINS" in result.stderr


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
