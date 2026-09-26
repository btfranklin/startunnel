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
from django.test import override_settings

from accounts.models import User
from agents.models import AgentCredential
from core.management.commands import provision_live_agent_proof, provision_load_profile
from tunnels.management.commands.maintenance_loop import (
    Command as MaintenanceCommand,
)
from tunnels.management.commands.maintenance_loop import (
    _connection_kwargs as maintenance_connection_kwargs,
)
from tunnels.management.commands.maintenance_loop import (
    _should_report_cycle,
)
from tunnels.models import Tunnel
from tunnels.services import create_tunnel

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
    output = StringIO()
    call_command("inspect_runtime", stdout=output)
    document = json.loads(output.getvalue())
    assert document["secrets"] == "redacted"
    assert document["identity"] == {"provider": "local"}
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
        "tunnels.management.commands.maintenance_loop.cleanup_once",
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
        "tunnels.management.commands.maintenance_loop.next_deadline",
        lambda: __import__("django.utils.timezone", fromlist=["now"]).now(),
    )
    command._sqlite_loop(stop)  # type: ignore[arg-type]
    assert waits == [0]


def test_postgres_loop_drains_due_work_and_records_notification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.has_due_work", lambda: next(due)
    )
    monkeypatch.setattr("tunnels.management.commands.maintenance_loop.next_deadline", lambda: None)
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.record_notification_wake",
        lambda: wakes.append(True),
    )
    command._postgres_loop(Listener(), stop)  # type: ignore[arg-type]
    assert wakes == [True]


def test_postgres_loop_does_not_busy_loop_after_delete_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "tunnels.management.commands.maintenance_loop.has_due_work",
        lambda: (_ for _ in ()).throw(AssertionError("must not drain failed work")),
    )
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.next_deadline",
        lambda: __import__("django.utils.timezone", fromlist=["now"]).now(),
    )
    command._postgres_loop(Listener(), stop)  # type: ignore[arg-type]


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

    monkeypatch.setattr("tunnels.management.commands.maintenance_loop.threading.Event", Stop)
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.signal.signal", lambda *args: None
    )
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.psycopg.connect",
        lambda **kwargs: (_ for _ in ()).throw(psycopg.OperationalError("down")),
    )
    errors: list[str] = []
    monkeypatch.setattr(
        "tunnels.management.commands.maintenance_loop.record_maintenance_error",
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
    monkeypatch.setattr(
        provision_live_agent_proof,
        "_validate_output_path",
        lambda raw_path: Path(raw_path),
    )
    first_path = tmp_path / "first.env"
    output = StringIO()
    call_command("provision_live_agent_proof", output=str(first_path), stdout=output)
    assert first_path.stat().st_mode & 0o777 == 0o600
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
    call_command(
        "provision_live_agent_proof",
        output=str(tmp_path / "second.env"),
        stdout=StringIO(),
    )
    assert not AgentCredential.objects.filter(pk__in=old_ids, revoked_at__isnull=True).exists()
    assert Tunnel.objects.get().state == Tunnel.State.RETIRED
    assert user.created_agent_credentials.filter(revoked_at__isnull=True).count() == 3


def test_live_proof_output_validation_and_mount_parser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CommandError, match="absolute"):
        provision_live_agent_proof._validate_output_path("relative.env")
    with pytest.raises(CommandError, match="does not exist"):
        provision_live_agent_proof._validate_output_path(str(tmp_path / "missing" / "x"))
    existing = tmp_path / "existing"
    existing.write_text("x", encoding="utf-8")
    with pytest.raises(CommandError, match="already exists"):
        provision_live_agent_proof._validate_output_path(str(existing))
    monkeypatch.setattr(provision_live_agent_proof, "_tmpfs_mount_for", lambda path: None)
    with pytest.raises(CommandError, match="verified tmpfs"):
        provision_live_agent_proof._validate_output_path(str(tmp_path / "new"))
    monkeypatch.setattr(provision_live_agent_proof, "_tmpfs_mount_for", lambda path: tmp_path)
    assert (
        provision_live_agent_proof._validate_output_path(str(tmp_path / "new")) == tmp_path / "new"
    )
    assert (
        provision_live_agent_proof._decode_mount_path(r"/safe\040space\134slash")
        == "/safe space\\slash"
    )


@override_settings(DEBUG=True)
def test_live_proof_removes_partial_output_after_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STARTUNNEL_LIVE_AGENT_TESTS", "1")
    path = tmp_path / "failed.env"
    monkeypatch.setattr(provision_live_agent_proof, "_validate_output_path", lambda raw: Path(raw))

    def fail_write(target: Path, values: dict[str, str]) -> None:
        del values
        target.write_text("partial", encoding="utf-8")
        raise OSError("write failed")

    monkeypatch.setattr(provision_live_agent_proof, "_write_environment", fail_write)
    with pytest.raises(OSError, match="write failed"):
        call_command("provision_live_agent_proof", output=str(path))
    assert not path.exists()


@pytest.mark.parametrize(
    ("users", "credentials", "message"),
    [(0, 1, "--users"), (5001, 1, "--users"), (1, 0, "--credentials"), (1, 5001, "--credentials")],
)
def test_load_fixture_counts_are_bounded(
    users: int, credentials: int, message: str, settings: Any
) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 5000
    with pytest.raises(CommandError, match=message):
        provision_load_profile._validate_counts(users=users, credentials=credentials)


def test_load_fixture_count_respects_instance_limit(settings: Any) -> None:
    settings.STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT = 1
    with pytest.raises(CommandError, match="instance credential limit"):
        provision_load_profile._validate_counts(users=1, credentials=2)


def test_load_manifest_paths_and_parser_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(CommandError, match="absolute"):
        provision_load_profile._validated_new_tmpfs_path("relative.json")
    candidate = tmp_path / "new.json"
    monkeypatch.setattr(provision_load_profile, "_tmpfs_mount_for", lambda path: tmp_path)
    assert provision_load_profile._validated_new_tmpfs_path(str(candidate)) == candidate
    with pytest.raises(CommandError, match="absolute"):
        provision_load_profile._validated_existing_tmpfs_path("relative.json")
    with pytest.raises(CommandError, match="does not exist"):
        provision_load_profile._validated_existing_tmpfs_path(str(candidate))
    manifest = tmp_path / "manifest.json"
    run_id = uuid4()
    manifest.write_text(
        json.dumps(
            {
                "credential_count": 0,
                "credentials": [],
                "run_id": str(run_id),
                "user_count": 0,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(manifest, 0o600)
    assert provision_load_profile._validated_existing_tmpfs_path(str(manifest)) == manifest
    assert provision_load_profile._load_run_id(manifest) == run_id
    manifest.write_text('{"run_id":"bad"}', encoding="utf-8")
    with pytest.raises(CommandError, match="not valid"):
        provision_load_profile._load_run_id(manifest)


@override_settings(DEBUG=True, STARTUNNEL_ACTIVE_CREDENTIAL_LIMIT=10)
def test_load_fixture_command_provisions_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STARTUNNEL_LOAD_TESTS", "1")
    monkeypatch.setattr(provision_load_profile, "_validated_new_tmpfs_path", lambda raw: Path(raw))
    monkeypatch.setattr(
        provision_load_profile, "_validated_existing_tmpfs_path", lambda raw: Path(raw)
    )
    path = tmp_path / "load.json"
    output = StringIO()
    call_command(
        "provision_load_profile",
        output=str(path),
        users=2,
        credentials=3,
        stdout=output,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["user_count"] == 2 and document["credential_count"] == 3
    assert "st_" not in output.getvalue()
    run_id = UUID(document["run_id"])
    call_command("provision_load_profile", cleanup_manifest=str(path), stdout=StringIO())
    assert not path.exists()
    assert not User.objects.filter(username__startswith=f"l-{run_id.hex}", is_active=True).exists()
    assert not AgentCredential.objects.filter(
        name__startswith=provision_load_profile._fixture_prefix(run_id),
        revoked_at__isnull=True,
    ).exists()


def test_load_fixture_command_requires_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STARTUNNEL_LOAD_TESTS", raising=False)
    with pytest.raises(CommandError, match="STARTUNNEL_LOAD_TESTS=1"):
        call_command("provision_load_profile", output=str(tmp_path / "load.json"))
