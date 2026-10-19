"""Operational commands are deterministic and do not print secrets."""

from __future__ import annotations

import json
import os
from io import StringIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import override_settings

from accounts.models import User
from agents.models import AgentCredential
from agents.services import CredentialError, authenticate_key
from core.management.commands import provision_live_agent_proof, provision_load_profile
from core.management.commands.maintenance_loop import (
    Command as MaintenanceCommand,
)
from core.management.commands.maintenance_loop import (
    _connection_kwargs as maintenance_connection_kwargs,
)
from core.management.commands.maintenance_loop import (
    _should_report_cycle,
)
from tunnels.cycles import create_tunnel
from tunnels.models import Tunnel

pytestmark = pytest.mark.django_db


def test_export_openapi_writes_valid_document(tmp_path: Path) -> None:
    destination = tmp_path / "openapi.json"
    output = StringIO()
    call_command("export_openapi", output=str(destination), stdout=output)
    schema = json.loads(destination.read_text(encoding="utf-8"))
    assert schema["info"]["title"] == "StarTunnel API"
    assert "/api/v1/messages" in schema["paths"]
    assert "Wrote" in output.getvalue()


def test_runtime_inspection_redacts_secrets(settings: Any) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 75
    output = StringIO()
    call_command("inspect_runtime", stdout=output)
    document = json.loads(output.getvalue())
    assert document["secrets"] == "redacted"
    assert document["identity"] == {"provider": "local"}
    assert document["limits"]["active_credentials"] == 75
    assert settings.SECRET_KEY not in output.getvalue()
    assert settings.API_KEY_PEPPER not in output.getvalue()


def test_runtime_inspection_handles_database_and_status_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.management.commands.inspect_runtime.connection.cursor",
        lambda: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    monkeypatch.setattr(
        "core.management.commands.inspect_runtime.read_maintenance_status",
        lambda: (_ for _ in ()).throw(RuntimeError("status down")),
    )
    output = StringIO()
    call_command("inspect_runtime", stdout=output)
    document = json.loads(output.getvalue())
    assert document["migrations"] == "unavailable"
    assert document["maintenance"] == "unavailable"


def test_maintenance_once_outputs_safe_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.cleanup_once",
        lambda: {"cycles_closed": 2},
    )
    output = StringIO()
    call_command("maintenance_loop", once=True, stdout=output)
    assert json.loads(output.getvalue()) == {
        "event": "maintenance_cycle",
        "counts": {"cycles_closed": 2},
    }
    assert _should_report_cycle({"cycles_closed": 0}, once=False) is False
    assert _should_report_cycle({"cycles_closed": 1}, once=False) is True


def test_maintenance_connection_shape(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "NAME": "db",
        "USER": "user",
        "PASSWORD": "pass",
        "HOST": "host",
        "PORT": "5432",
    }.items():
        monkeypatch.setitem(settings.DATABASES["default"], name, value)
    assert maintenance_connection_kwargs() == {
        "dbname": "db",
        "user": "user",
        "password": "pass",
        "host": "host",
        "port": "5432",
        "connect_timeout": 5,
    }


def test_sqlite_maintenance_loop_waits_until_deadline(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.sqlite3")
    waits: list[float] = []

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            self.stopped = True
            return True

        def set(self) -> None:
            self.stopped = True

    stop = Stop()
    command = MaintenanceCommand()
    monkeypatch.setattr(command, "_reconcile", lambda once: {"cycles_closed": 0})
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.next_deadline",
        lambda **kwargs: __import__("django.utils.timezone", fromlist=["now"]).now(),
    )
    command._sqlite_loop(stop)  # type: ignore[arg-type]
    assert waits == [0]


def test_postgres_loop_drains_due_work_and_records_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop._wait_for_notification",
        lambda listener, stop_socket, timeout: bool(
            listener.notifies(timeout=timeout, stop_after=1)
        ),
    )
    reconciliations = iter(
        [
            {"cycles_closed": 1, "cycle_delete_failures": 0},
            {"cycles_closed": 0, "cycle_delete_failures": 0},
        ]
    )
    due = iter([True, False])
    wakes: list[bool] = []

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

    class Listener:
        def notifies(self, *, timeout: float, stop_after: int) -> list[object]:
            assert timeout >= 0 and stop_after == 1
            stop.stopped = True
            return [object()]

    stop = Stop()
    command = MaintenanceCommand()
    monkeypatch.setattr(command, "_reconcile", lambda once: next(reconciliations))
    monkeypatch.setattr("core.management.commands.maintenance_loop.has_due_work", lambda: next(due))
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.next_deadline", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.record_notification_wake",
        lambda: wakes.append(True),
    )
    command._postgres_loop(Listener(), stop, None)  # type: ignore[arg-type]
    assert wakes == [True]


def test_postgres_loop_does_not_busy_loop_after_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop._wait_for_notification",
        lambda listener, stop_socket, timeout: bool(
            listener.notifies(timeout=timeout, stop_after=1)
        ),
    )

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

    class Listener:
        def notifies(self, *, timeout: float, stop_after: int) -> list[object]:
            assert timeout > 0 and stop_after == 1
            stop.stopped = True
            return []

    stop = Stop()
    command = MaintenanceCommand()
    monkeypatch.setattr(
        command,
        "_reconcile",
        lambda once: {"cycle_delete_failures": 1},
    )
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.has_due_work",
        lambda: (_ for _ in ()).throw(AssertionError("must not drain failed work")),
    )

    def deadline_after_failure(*, skip_overdue_deletions: bool = False) -> None:
        assert skip_overdue_deletions

    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.next_deadline", deadline_after_failure
    )
    command._postgres_loop(Listener(), stop, None)  # type: ignore[arg-type]


def test_maintenance_recovers_from_orm_errors_with_backoff(
    settings: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop._wait_for_notification",
        lambda listener, stop_socket, timeout: bool(
            listener.notifies(timeout=timeout, stop_after=1)
        ),
    )
    from core.logging import SafeJsonFormatter

    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.postgresql")
    waits: list[float] = []
    closed: list[bool] = []
    errors: list[str] = []
    attempts: list[bool] = []

    class Stop:
        stopped = False

        def is_set(self) -> bool:
            return self.stopped

        def set(self) -> None:
            self.stopped = True

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            if len(waits) == 3:
                self.stopped = True
            return self.stopped

    class Listener:
        def __enter__(self) -> Listener:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def execute(self, *args: Any) -> Listener:
            return self

        def fetchone(self) -> tuple[bool]:
            return (True,)

        def notifies(self, **kwargs: Any) -> list[object]:
            return []

    stop = Stop()

    def cleanup() -> dict[str, int]:
        attempts.append(True)
        if len(attempts) != 3:
            raise OperationalError("Synthetic SQL text must not enter logs.")
        return {"cycles_closed": 0}

    module = "core.management.commands.maintenance_loop."
    monkeypatch.setattr(module + "threading.Event", lambda: stop)
    monkeypatch.setattr(module + "signal.signal", lambda *args: None)
    monkeypatch.setattr(module + "psycopg.connect", lambda **kwargs: Listener())
    monkeypatch.setattr(module + "cleanup_once", cleanup)
    monkeypatch.setattr(module + "next_deadline", lambda **kwargs: None)
    monkeypatch.setattr(module + "close_old_connections", lambda: closed.append(True))
    monkeypatch.setattr(module + "record_maintenance_error", errors.append)
    MaintenanceCommand().handle(once=False)
    assert len(attempts) == 4
    assert waits == [1.0, 2.0, 1.0]
    assert closed == [True, True, True]
    assert errors == ["OperationalError", "OperationalError", "OperationalError"]
    events = [json.loads(SafeJsonFormatter().format(record)) for record in caplog.records]
    assert len(events) == 3
    assert all(event["exception_type"] == "OperationalError" for event in events)
    assert "Synthetic SQL text" not in json.dumps(events)


def test_maintenance_handle_backs_off_after_postgres_error(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(settings.DATABASES["default"], "ENGINE", "django.db.backends.postgresql")
    waits: list[float] = []

    class Stop:
        def is_set(self) -> bool:
            return False

        def wait(self, seconds: float) -> bool:
            waits.append(seconds)
            return True

        def set(self) -> None:
            return None

    monkeypatch.setattr("core.management.commands.maintenance_loop.threading.Event", Stop)
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.signal.signal", lambda *args: None
    )
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.psycopg.connect",
        lambda **kwargs: (_ for _ in ()).throw(psycopg.OperationalError("down")),
    )
    errors: list[str] = []
    monkeypatch.setattr(
        "core.management.commands.maintenance_loop.record_maintenance_error",
        errors.append,
    )
    MaintenanceCommand().handle(once=False)
    assert waits == [1.0]
    assert errors == ["OperationalError"]


def test_live_proof_requires_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STARTUNNEL_LIVE_AGENT_TESTS", raising=False)
    with pytest.raises(CommandError, match="STARTUNNEL_LIVE_AGENT_TESTS=1"):
        call_command("provision_live_agent_proof", output=str(tmp_path / "proof.env"))


@override_settings(DEBUG=True)
def test_live_proof_replaces_prior_fixture_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STARTUNNEL_LIVE_AGENT_TESTS", "1")
    first_path = tmp_path / "first.env"
    output = StringIO()
    call_command("provision_live_agent_proof", output=str(first_path), stdout=output)
    assert first_path.stat().st_mode & 0o777 == 0o600
    first_contents = first_path.read_bytes()
    with pytest.raises(CommandError, match="already exists"):
        call_command("provision_live_agent_proof", output=str(first_path), stdout=StringIO())
    assert first_path.read_bytes() == first_contents
    names = {line.partition("=")[0] for line in first_path.read_text(encoding="utf-8").splitlines()}
    assert names == {
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
        "STARTUNNEL_RECEIVER_KEYS",
        "STARTUNNEL_SENDER_ID",
        "STARTUNNEL_RECEIVER_ID",
    }
    assert "st_" not in output.getvalue()
    user = User.objects.get(username="live-agent-proof")
    old_ids = set(user.created_agent_credentials.values_list("id", flat=True))
    sender = user.created_agent_credentials.get(name="Live proof sender")
    create_tunnel(
        credential=sender,
        idempotency_key="live-proof-old-tunnel",
        root_content={"type": "text", "text": "Old fixture."},
    )
    second_path = tmp_path / "second.env"
    call_command("provision_live_agent_proof", output=str(second_path), stdout=StringIO())
    assert second_path.stat().st_mode & 0o777 == 0o600
    assert not AgentCredential.objects.filter(pk__in=old_ids, revoked_at__isnull=True).exists()
    assert Tunnel.objects.get().state == Tunnel.State.RETIRED
    assert user.created_agent_credentials.filter(revoked_at__isnull=True).count() == 3


def test_live_proof_output_validation(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="absolute"):
        provision_live_agent_proof._validate_output_path("relative.env")
    with pytest.raises(CommandError, match="does not exist"):
        provision_live_agent_proof._validate_output_path(str(tmp_path / "missing" / "x"))
    existing = tmp_path / "existing"
    existing.write_text("x", encoding="utf-8")
    with pytest.raises(CommandError, match="already exists"):
        provision_live_agent_proof._validate_output_path(str(existing))
    link = tmp_path / "linked.env"
    link.symlink_to(existing)
    with pytest.raises(CommandError, match="already exists"):
        provision_live_agent_proof._validate_output_path(str(link))
    candidate = tmp_path / "new.env"
    assert provision_live_agent_proof._validate_output_path(str(candidate)) == candidate


def test_credential_output_writers_are_private_and_exclusive(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    provision_load_profile._write_manifest(manifest, {"run_id": "safe"})
    assert manifest.stat().st_mode & 0o777 == 0o600
    manifest_contents = manifest.read_bytes()
    with pytest.raises(FileExistsError):
        provision_load_profile._write_manifest(manifest, {"run_id": "changed"})
    assert manifest.read_bytes() == manifest_contents

    environment = tmp_path / "proof.env"
    provision_live_agent_proof._write_environment(environment, {"STARTUNNEL_AGENT_KEY": "safe"})
    assert environment.stat().st_mode & 0o777 == 0o600
    environment_contents = environment.read_bytes()
    with pytest.raises(FileExistsError):
        provision_live_agent_proof._write_environment(
            environment, {"STARTUNNEL_AGENT_KEY": "changed"}
        )
    assert environment.read_bytes() == environment_contents


@override_settings(DEBUG=True)
def test_live_proof_removes_partial_output_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STARTUNNEL_LIVE_AGENT_TESTS", "1")
    path = tmp_path / "failed.env"

    def fail_write(target: Path, values: dict[str, str]) -> None:
        del values
        target.write_text("partial", encoding="utf-8")
        raise OSError("write failed")

    monkeypatch.setattr(provision_live_agent_proof, "_write_environment", fail_write)
    with pytest.raises(OSError, match="write failed"):
        call_command("provision_live_agent_proof", output=str(path))
    assert not path.exists()


@pytest.mark.parametrize(
    ("credentials", "message"),
    [(0, "--credentials"), (5001, "--credentials")],
)
def test_load_fixture_counts_are_bounded(credentials: int, message: str, settings: Any) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 5000
    with pytest.raises(CommandError, match=message):
        provision_load_profile._validate_counts(credentials=credentials)


def test_load_fixture_count_respects_instance_limit(settings: Any) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 1
    with pytest.raises(CommandError, match="instance credential limit"):
        provision_load_profile._validate_counts(credentials=2)


def test_load_manifest_paths_and_parser_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="absolute"):
        provision_load_profile._validated_new_output_path("relative.json")
    candidate = tmp_path / "new.json"
    assert provision_load_profile._validated_new_output_path(str(candidate)) == candidate
    output_link = tmp_path / "new-link.json"
    output_link.symlink_to(candidate)
    with pytest.raises(CommandError, match="already exists"):
        provision_load_profile._validated_new_output_path(str(output_link))
    with pytest.raises(CommandError, match="absolute"):
        provision_load_profile._validated_existing_manifest_path("relative.json")
    with pytest.raises(CommandError, match="does not exist"):
        provision_load_profile._validated_existing_manifest_path(str(candidate))
    with pytest.raises(CommandError, match="regular file"):
        provision_load_profile._validated_existing_manifest_path(str(tmp_path))
    manifest = tmp_path / "manifest.json"
    run_id = uuid4()
    manifest.write_text(
        json.dumps(
            {
                "credential_count": 0,
                "credentials": [],
                "run_id": str(run_id),
            }
        ),
        encoding="utf-8",
    )
    os.chmod(manifest, 0o400)
    linked_manifest = tmp_path / "manifest-link.json"
    linked_manifest.symlink_to(manifest)
    assert (
        provision_load_profile._validated_existing_manifest_path(str(linked_manifest)) == manifest
    )
    assert provision_load_profile._load_run_id(manifest) == run_id
    manifest.chmod(0o600)
    manifest.write_text('{"run_id":"bad"}', encoding="utf-8")
    with pytest.raises(CommandError, match="not valid"):
        provision_load_profile._load_run_id(manifest)


@override_settings(DEBUG=True, STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT=10)
def test_load_fixture_command_provisions_and_cleans_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credential_factory: Any,
) -> None:
    monkeypatch.setenv("STARTUNNEL_LOAD_TESTS", "1")
    path = tmp_path / "load.json"
    output = StringIO()
    call_command(
        "provision_load_profile",
        output=str(path),
        credentials=3,
        stdout=output,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert path.stat().st_mode & 0o777 == 0o600
    original_contents = path.read_bytes()
    with pytest.raises(CommandError, match="already exists"):
        call_command(
            "provision_load_profile",
            output=str(path),
            credentials=3,
            stdout=StringIO(),
        )
    assert path.read_bytes() == original_contents
    assert document["credential_count"] == 3
    assert "st_" not in output.getvalue()
    run_id = UUID(document["run_id"])
    fixture_username = f"l-{run_id.hex}"
    fixture_user = User.objects.get(username=fixture_username)
    assert fixture_user.is_active
    assert not fixture_user.has_usable_password()
    fixture_credentials = list(
        AgentCredential.objects.filter(
            name__startswith=provision_load_profile._fixture_prefix(run_id)
        )
    )
    assert len(fixture_credentials) == 3
    assert {credential.created_by_id for credential in fixture_credentials} == {fixture_user.id}
    authenticated = [authenticate_key(entry["key"]) for entry in document["credentials"]]
    assert {credential.id for credential in authenticated} == {
        credential.id for credential in fixture_credentials
    }

    fixture_tunnel = create_tunnel(
        credential=authenticated[0],
        idempotency_key="load-fixture-owned-tunnel",
        root_content={"type": "text", "text": "Fixture-owned tunnel."},
    )
    unrelated_credential, _ = credential_factory()
    unrelated_tunnel = create_tunnel(
        credential=unrelated_credential,
        idempotency_key="load-unrelated-tunnel",
        root_content={"type": "text", "text": "Unrelated tunnel."},
    )

    call_command("provision_load_profile", cleanup_manifest=str(path), stdout=StringIO())
    assert not path.exists()
    fixture_user.refresh_from_db()
    assert not fixture_user.is_active
    fixture_credentials = list(
        AgentCredential.objects.filter(
            name__startswith=provision_load_profile._fixture_prefix(run_id)
        )
    )
    assert len(fixture_credentials) == 3
    assert all(credential.revoked_at is not None for credential in fixture_credentials)
    assert all(not credential.is_available for credential in fixture_credentials)
    with pytest.raises(CredentialError, match="invalid"):
        authenticate_key(document["credentials"][0]["key"])
    assert Tunnel.objects.get(pk=fixture_tunnel.tunnel.pk).state == Tunnel.State.RETIRED
    assert Tunnel.objects.get(pk=unrelated_tunnel.tunnel.pk).state == Tunnel.State.ACTIVE
    unrelated_credential.refresh_from_db()
    assert unrelated_credential.is_available


def test_load_fixture_command_requires_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STARTUNNEL_LOAD_TESTS", raising=False)
    with pytest.raises(CommandError, match="STARTUNNEL_LOAD_TESTS=1"):
        call_command("provision_load_profile", output=str(tmp_path / "load.json"))
