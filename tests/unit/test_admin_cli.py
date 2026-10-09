from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

CLI_PATH = Path(__file__).resolve().parents[2] / "cli" / "star_tunnel.py"
spec = importlib.util.spec_from_file_location("admin_cli_under_test", CLI_PATH)
assert spec and spec.loader
cli = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cli
spec.loader.exec_module(cli)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STARTUNNEL_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY", "sta_disposable-test-value")
    monkeypatch.delenv("STARTUNNEL_ADMIN_KEY_FILE", raising=False)


def stdin(monkeypatch: pytest.MonkeyPatch, value: dict[str, Any]) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(value).encode())))


def arguments(*values: str) -> Any:
    return cli.build_parser().parse_args(["admin", *values])


def test_schema_is_available_without_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARTUNNEL_BASE_URL", raising=False)
    result = cli.run_admin(arguments("schema"))
    assert result["openapi_path"] == "/api/v1/openapi.json"
    assert len(result["commands"]) == len(cli.ADMIN_COMMANDS)
    for command in result["commands"]:
        parsed = (
            arguments(*command["command"].split()[1:])
            if not (command["idempotency_key_required"] or "{id}" in command["path"])
            else None
        )
        if parsed:
            assert parsed.admin_command == command["command"][6:]


def test_admin_key_file_requires_private_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = tmp_path / "key"
    filename.write_text("sta_disposable-test-value\n")
    monkeypatch.delenv("STARTUNNEL_ADMIN_KEY", raising=False)
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY_FILE", str(filename))
    filename.chmod(0o600)
    assert cli.require_admin_key() == "sta_disposable-test-value"
    filename.chmod(0o644)
    with pytest.raises(cli.CliError):
        cli.require_admin_key()
    filename.chmod(0o600)
    symlink = tmp_path / "link"
    symlink.symlink_to(filename)
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY_FILE", str(symlink))
    with pytest.raises(cli.CliError):
        cli.require_admin_key()


def test_admin_key_configuration_conflict(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STARTUNNEL_ADMIN_KEY_FILE", "/missing")
    with pytest.raises(cli.CliError):
        cli.require_admin_key()


def test_secret_saved_privately_and_removed_from_result(
    configured: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = tmp_path / "issued.key"
    stdin(monkeypatch, {"name": "worker"})
    monkeypatch.setattr(
        cli.StarTunnelTransport,
        "request",
        lambda *a, **kw: {
            "resource": {"id": "safe-id"},
            "operation": {"id": "receipt"},
            "secret": "st_disposable-issued-value",
        },
    )
    result = cli.run_admin(
        arguments(
            "agents", "create", "--idempotency-key", "test-key-1", "--secret-output", str(filename)
        )
    )
    assert filename.read_text() == "st_disposable-issued-value\n"
    assert filename.stat().st_mode & 0o777 == 0o600
    assert "secret" not in result
    assert result["secret_output"] == str(filename)


def test_existing_secret_file_never_deleted_or_requested(
    configured: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = tmp_path / "issued.key"
    filename.write_text("keep this")
    stdin(monkeypatch, {"name": "worker"})

    def unexpected(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("No HTTP request may precede secret file reservation.")

    monkeypatch.setattr(cli.StarTunnelTransport, "request", unexpected)
    with pytest.raises(cli.CliError):
        cli.run_admin(
            arguments(
                "agents",
                "create",
                "--idempotency-key",
                "test-key-1",
                "--secret-output",
                str(filename),
            )
        )
    assert filename.read_text() == "keep this"


def test_failed_request_removes_reserved_file(
    configured: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = tmp_path / "issued.key"
    stdin(monkeypatch, {"name": "worker"})

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise cli.CliError("state_conflict", status=409)

    monkeypatch.setattr(cli.StarTunnelTransport, "request", fail)
    with pytest.raises(cli.CliError):
        cli.run_admin(
            arguments(
                "agents",
                "create",
                "--idempotency-key",
                "test-key-1",
                "--secret-output",
                str(filename),
            )
        )
    assert not filename.exists()


@pytest.mark.parametrize("status", [429, 502, 503, 504])
def test_transient_retry_retains_body_and_key(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    transport = cli.StarTunnelTransport("http://127.0.0.1:8000", "test")
    calls = []
    sleeps: list[float] = []

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        if len(calls) < 3:
            raise cli.CliError("http_error", status=status, retry_after=0.25)
        return {"resource": {"id": "done"}}

    monkeypatch.setattr(transport, "_request_once", request)
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    result = transport.request(
        "POST",
        "/api/v1/admin/keys/create",
        body={"name": "worker"},
        idempotency_key="retry-key",
        expected={201},
        retry=True,
    )
    assert result["resource"]["id"] == "done"
    assert calls[0] == calls[1] == calls[2]
    assert sleeps == [0.25, 0.25]


@pytest.mark.parametrize("status", [400, 401, 403, 409])
def test_permanent_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    transport = cli.StarTunnelTransport("http://127.0.0.1:8000", "test")
    calls = []

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise cli.CliError("http_error", status=status)

    monkeypatch.setattr(transport, "_request_once", request)
    with pytest.raises(cli.CliError):
        transport.request("GET", "/api/v1/admin/me", expected={200}, retry=True)
    assert len(calls) == 1


def test_doctor_failed_health_is_error(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.StarTunnelTransport, "request", lambda *a, **kw: {"healthy": False})
    with pytest.raises(cli.CliError, match="health_check_failed"):
        cli.run_admin(arguments("doctor"))


def test_unsafe_uuid_rejected_before_request(configured: None) -> None:
    with pytest.raises(cli.CliError):
        cli.run_admin(arguments("accounts", "get", "../keys"))


def test_retry_after_date_and_long_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.time, "time", lambda: 0)
    assert cli._retry_after_seconds("Thu, 01 Jan 1970 00:00:04 GMT") == 4
    assert cli._retry_after_seconds("999999") == 999999
    assert cli._retry_after_seconds("nan") is None
    assert cli._retry_after_seconds("inf") is None
    assert cli._retry_after_seconds("not a date") is None


def test_connection_retry_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = cli.StarTunnelTransport("http://127.0.0.1:8000", "test")
    calls = []

    def fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise cli.CliError("connection_failed")

    monkeypatch.setattr(transport, "_request_once", fail)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    with pytest.raises(cli.CliError):
        transport.request("GET", "/api/v1/admin/me", expected={200}, retry=True)
    assert len(calls) == 3


def test_secret_file_write_failure_is_safe(
    configured: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    filename = tmp_path / "issued.key"
    stdin(monkeypatch, {"name": "worker"})
    monkeypatch.setattr(
        cli.StarTunnelTransport,
        "request",
        lambda *a, **kw: {
            "resource": {"id": "safe-id"},
            "secret": "st_disposable-issued-value",
        },
    )

    def fail(descriptor: int) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(cli.os, "fsync", fail)
    with pytest.raises(cli.CliError, match="secret_output_failed"):
        cli.run_admin(
            arguments(
                "agents",
                "create",
                "--idempotency-key",
                "test-key-1",
                "--secret-output",
                str(filename),
            )
        )
    assert not filename.exists()


def test_list_filters_are_encoded(configured: None, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return {"items": []}

    monkeypatch.setattr(cli.StarTunnelTransport, "request", request)
    cli.run_admin(
        arguments(
            "keys",
            "list",
            "--limit",
            "20",
            "--cursor",
            "a+b/c",
            "--admin-id",
            "test-admin",
            "--state",
            "active",
        )
    )
    assert calls[0][2] == (
        "/api/v1/admin/keys?limit=20&cursor=a%2Bb%2Fc&admin_id=test-admin&state=active"
    )


def test_audit_action_filter_preserves_admin_dispatch(
    configured: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = []

    def request(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return {"items": []}

    monkeypatch.setattr(cli.StarTunnelTransport, "request", request)
    assert cli.main(["admin", "audit", "list", "--action", "keys.revoke"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {"items": []}
    assert calls[0][2] == "/api/v1/admin/audit?action=keys.revoke"


def test_long_retry_after_returns_control_without_early_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = cli.StarTunnelTransport("http://127.0.0.1:8000", "test")
    calls: list[object] = []

    def fail(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        raise cli.CliError("http_error", status=429, retry_after=60)

    monkeypatch.setattr(transport, "_request_once", fail)
    sleeps: list[float] = []
    monkeypatch.setattr(cli.time, "sleep", sleeps.append)
    with pytest.raises(cli.CliError) as error:
        transport.request("GET", "/api/v1/admin/me", expected={200}, retry=True)
    assert error.value.retry_after == 60
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_nonfinite_retry_after_is_ignored(value: str) -> None:
    assert cli._retry_after_seconds(value) is None
