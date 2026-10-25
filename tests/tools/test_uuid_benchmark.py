"""UUID benchmark input checks fail before database access."""

from __future__ import annotations

import pytest
from scripts import benchmark_uuid


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {
            "STARTUNNEL_ENV": "development",
            "DJANGO_SETTINGS_MODULE": "startunnel.settings.test",
            "TEST_DATABASE_URL": "postgresql://private.example/test",
        },
        {
            "STARTUNNEL_ENV": "test",
            "DJANGO_SETTINGS_MODULE": "startunnel.settings.development",
            "TEST_DATABASE_URL": "postgresql://private.example/test",
        },
        {
            "STARTUNNEL_ENV": "test",
            "DJANGO_SETTINGS_MODULE": "startunnel.settings.test",
            "TEST_DATABASE_URL": "sqlite:///private.db",
        },
    ],
)
def test_benchmark_refuses_non_test_or_non_postgresql_environment(
    environ: dict[str, str],
) -> None:
    with pytest.raises(RuntimeError):
        benchmark_uuid._validate_environment(environ)


@pytest.mark.parametrize(("rows", "trials"), [(0, 1), (100_001, 1), (1, 0), (1, 10)])
def test_benchmark_rejects_unbounded_options(rows: int, trials: int) -> None:
    with pytest.raises(ValueError):
        benchmark_uuid._validate_options(rows, trials)


def test_main_reports_safe_failure_outside_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("STARTUNNEL_ENV", "production")
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "startunnel.settings.production")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql://private-key@private.example/db")

    assert benchmark_uuid.main(["--rows", "1", "--trials", "1"]) == 1
    output = capsys.readouterr()
    assert "private" not in output.out + output.err
