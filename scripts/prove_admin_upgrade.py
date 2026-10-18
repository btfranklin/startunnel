"""Prove forward migrations on a restored initial PostgreSQL baseline."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = [(name, "0001_initial") for name in ("accounts", "agents", "core", "tunnels")]


def baseline(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth.hashers import make_password
    from django.db import connection, transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    from tunnels.codec import encode_token

    executor = MigrationExecutor(connection)
    executor.migrate(BASELINE)
    apps = executor.loader.project_state(BASELINE).apps
    user_model = apps.get_model("accounts", "User")
    credential_model = apps.get_model("agents", "AgentCredential")
    tunnel_model = apps.get_model("tunnels", "Tunnel")
    address_model = apps.get_model("tunnels", "TunnelAddress")
    cycle_model = apps.get_model("tunnels", "Cycle")
    message_model = apps.get_model("tunnels", "Message")
    audit_model = apps.get_model("tunnels", "AuditEvent")
    password = secrets.token_urlsafe(32)
    agent_key = "st_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    with transaction.atomic():
        user = user_model.objects.create(username="upgrade-admin", password=make_password(password))
        credential = credential_model.objects.create(
            created_by=user,
            name="upgrade-agent",
            display_prefix=agent_key[:11],
            key_digest=hmac.digest(settings.API_KEY_PEPPER.encode(), agent_key.encode(), "sha256"),
        )
        tunnel = tunnel_model.objects.create(creator=credential, label="Migration proof")
        address_token = secrets.token_bytes(16)
        address_secret = secrets.token_urlsafe(32)
        address = encode_token(address_token)
        address_row = address_model.objects.create(
            tunnel=tunnel,
            generation=2,
            address_digest=hmac.digest(address_secret.encode(), address_token, "sha256"),
        )
        retired_token = secrets.token_bytes(16)
        retired_address = encode_token(retired_token)
        retired_row = address_model.objects.create(
            tunnel=tunnel,
            address_digest=hmac.digest(address_secret.encode(), retired_token, "sha256"),
            state="retired",
            retired_at=timezone.now(),
            retirement_reason="operator_rotation",
        )
        cycle = cycle_model.objects.create(
            tunnel=tunnel,
            creator=credential,
            number=1,
            expires_at=timezone.now() + timedelta(days=1),
            retention_seconds=2592000,
        )
        messages: list[Any] = []
        for sequence, content in enumerate(("Root retained", "Reply retained"), start=1):
            message = message_model.objects.create(
                tunnel=tunnel,
                cycle=cycle,
                parent=messages[0] if messages else None,
                sender=credential,
                sender_name=credential.name,
                sequence=sequence,
                depth=sequence - 1,
                payload_type="text",
                text_payload=content,
                byte_count=len(content.encode()),
                content_digest=hashlib.sha256(content.encode()).digest(),
            )
            messages.append(message)
        cycle.root_message = messages[0]
        cycle.next_sequence = 3
        cycle.message_count = 2
        cycle.save()
        audit = audit_model.objects.create(
            actor_user=user,
            action="human.created",
            target_type="user",
            target_id=user.pk,
            metadata={"historical": True},
        )
    snapshot_path.write_text(
        json.dumps(
            {
                "user_id": str(user.pk),
                "password": password,
                "password_hash": user.password,
                "credential_id": str(credential.pk),
                "agent_key": agent_key,
                "key_digest": bytes(credential.key_digest).hex(),
                "tunnel_id": str(tunnel.pk),
                "address_id": str(address_row.pk),
                "retired_address_id": str(retired_row.pk),
                "address_secret": address_secret,
                "addresses": {
                    str(address_row.pk): address,
                    str(retired_row.pk): retired_address,
                },
                "cycle_id": str(cycle.pk),
                "root_id": str(messages[0].pk),
                "reply_id": str(messages[1].pk),
                "audit_id": str(audit.pk),
            }
        ),
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)
    connection.close()


def upgraded(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection, transaction
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    from accounts.admin_access import authenticate_admin_key
    from accounts.models import User
    from agents.models import AgentCredential
    from agents.services import authenticate_key
    from tunnels.access import resolve_tunnel
    from tunnels.errors import TunnelUnavailable
    from tunnels.models import AuditEvent, Cycle, IdempotencyRecord, Message, TunnelAddress

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    executor = MigrationExecutor(connection)
    assert not any(
        app in {"accounts", "tunnels"} and name.startswith("0002")
        for app, name in executor.loader.applied_migrations
    )

    def rejected_conversion(message: str) -> None:
        try:
            candidate = MigrationExecutor(connection)
            candidate.migrate(candidate.loader.graph.leaf_nodes())
        except RuntimeError as error:
            assert message in str(error)
        else:
            raise AssertionError("An incomplete address conversion was accepted.")
        with connection.cursor() as cursor:
            columns = {
                column.name
                for column in connection.introspection.get_table_description(
                    cursor, "tunnels_tunneladdress"
                )
            }
        assert "address_digest" in columns and "address" not in columns

    os.environ.pop("STARTUNNEL_ADDRESS_MIGRATION_FILE", None)
    rejected_conversion("Supply a mode-0600")
    os.environ["STARTUNNEL_ADDRESS_MIGRATION_FILE"] = str(snapshot_path)
    snapshot_path.chmod(0o644)
    rejected_conversion("Supply a mode-0600")
    snapshot_path.chmod(0o600)
    original = snapshot_path.read_text(encoding="utf-8")
    wrong = dict(expected, address_secret="incorrect-proof-secret")
    snapshot_path.write_text(json.dumps(wrong), encoding="utf-8")
    rejected_conversion("Supply a mode-0600")
    snapshot_path.write_text(original, encoding="utf-8")
    pending = IdempotencyRecord.objects.create(
        credential_id=expected["credential_id"],
        operation="post_reply",
        key_digest=secrets.token_bytes(32),
        request_digest=secrets.token_bytes(32),
        expires_at=timezone.now() + timedelta(hours=1),
    )
    rejected_conversion("Stop writers")
    pending.expires_at = timezone.now() - timedelta(seconds=1)
    pending.save(update_fields=["expires_at"])
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
    finally:
        os.environ.pop("STARTUNNEL_ADDRESS_MIGRATION_FILE", None)
    user = User.objects.get(pk=expected["user_id"])
    assert user.password == expected["password_hash"] and user.check_password(expected["password"])
    credential = AgentCredential.objects.get(pk=expected["credential_id"])
    assert bytes(credential.key_digest).hex() == expected["key_digest"]
    assert str(authenticate_key(expected["agent_key"]).pk) == expected["credential_id"]
    address = expected["addresses"][expected["address_id"]]
    assert TunnelAddress.objects.get(pk=expected["address_id"]).address == address
    assert str(resolve_tunnel(credential=credential, address=address).pk) == expected["tunnel_id"]
    retired_address = expected["addresses"][expected["retired_address_id"]]
    retired = TunnelAddress.objects.get(pk=expected["retired_address_id"])
    assert retired.address == retired_address and retired.state == "retired"
    assert retired.generation == 1 and retired.retirement_reason == "operator_rotation"
    try:
        resolve_tunnel(credential=credential, address=retired_address)
    except TunnelUnavailable:
        pass
    else:
        raise AssertionError("A retired address became available after conversion.")
    root = Message.objects.get(pk=expected["root_id"])
    reply = Message.objects.get(pk=expected["reply_id"])
    cycle = Cycle.objects.get(pk=expected["cycle_id"])
    assert str(cycle.tunnel_id) == expected["tunnel_id"]
    assert cycle.root_message_id == root.pk and cycle.next_sequence == 3
    assert root.text_payload == "Root retained" and root.parent_id is None
    assert reply.text_payload == "Reply retained" and reply.parent_id == root.pk
    assert root.sequence == 1 and reply.sequence == 2 and reply.depth == 1
    assert root.session is None and reply.session is None
    assert root.sender_name == reply.sender_name == credential.name
    audit = AuditEvent.objects.get(pk=expected["audit_id"])
    assert audit.action == "human.created" and audit.metadata == {"historical": True}
    assert audit.actor_user_id == user.pk and audit.actor_admin_credential_id is None
    assert audit.channel == ""
    recovery_path = snapshot_path.parent / "recovery.key"
    call_command("recover_instance_admin", user.username, key_file=str(recovery_path), verbosity=0)
    assert recovery_path.stat().st_mode & 0o777 == 0o600
    recovered = authenticate_admin_key(recovery_path.read_text().strip())
    assert recovered.owner_id == user.pk
    assert AuditEvent.objects.filter(action="admin.recovered", channel="server").exists()
    with transaction.atomic():
        labeled = Message.objects.create(
            tunnel_id=expected["tunnel_id"],
            cycle=cycle,
            parent=reply,
            sender=credential,
            sender_name=credential.name,
            session="upgrade-proof",
            sequence=3,
            depth=2,
            payload_type="text",
            text_payload="Post-upgrade reply",
            byte_count=18,
            content_digest=hashlib.sha256(b"Post-upgrade reply").digest(),
        )
        cycle.next_sequence = 4
        cycle.message_count = 3
        cycle.save()
    assert Message.objects.get(pk=labeled.pk).session == "upgrade-proof"
    connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("baseline", "upgrade"))
    parser.add_argument("--snapshot", type=Path)
    arguments = parser.parse_args()
    if arguments.phase:
        if arguments.snapshot is None:
            parser.error("--snapshot is required with --phase")
        (baseline if arguments.phase == "baseline" else upgraded)(arguments.snapshot)
        return 0

    sys.path.insert(0, str(ROOT))
    from scripts.compose_isolation import IsolatedComposeProject, generated_project_name

    os.umask(0o077)
    project_name = generated_project_name("admin-upgrade")
    with tempfile.TemporaryDirectory(prefix="startunnel-admin-upgrade-") as directory:
        temporary = Path(directory).resolve()
        overlay = temporary / "postgres-port.yaml"
        overlay.write_text('services:\n  postgres:\n    ports: ["127.0.0.1::5432"]\n')

        class ProofProject(IsolatedComposeProject):
            def compose_files(self) -> tuple[str, ...]:
                return (*super().compose_files(), str(overlay))

        with ProofProject(project_name) as stack:
            stack.run("Start isolated PostgreSQL", "up", "--wait", "postgres", timeout_seconds=180)
            port = stack.capture("Local database port", "port", "postgres", "5432").rsplit(":", 1)[
                1
            ]
            for database in ("admin_upgrade_source", "admin_upgrade_restored"):
                stack.run(
                    "Create disposable proof database",
                    "exec",
                    "-T",
                    "postgres",
                    "createdb",
                    "--username",
                    "startunnel_admin",
                    database,
                    timeout_seconds=30,
                )
            environment = dict(stack.environment)
            environment.update(
                DJANGO_SETTINGS_MODULE="startunnel.settings.test",
                PYTHONPATH=str(ROOT / "src"),
            )
            snapshot = temporary / "snapshot.json"
            for phase, database in (
                ("baseline", "admin_upgrade_source"),
                ("upgrade", "admin_upgrade_restored"),
            ):
                if phase == "upgrade":
                    dump_directory = stack.capture(
                        "Create private container dump directory",
                        "exec",
                        "-T",
                        "postgres",
                        "mktemp",
                        "-d",
                    ).strip()
                    dump_file = str(Path(dump_directory) / "admin-upgrade.dump")
                    stack.run(
                        "Dump pre-change baseline",
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "--username",
                        "startunnel_admin",
                        "--format=custom",
                        "--file",
                        dump_file,
                        "admin_upgrade_source",
                        timeout_seconds=60,
                    )
                    stack.run(
                        "Restore pre-change baseline",
                        "exec",
                        "-T",
                        "postgres",
                        "pg_restore",
                        "--username",
                        "startunnel_admin",
                        "--dbname=admin_upgrade_restored",
                        "--exit-on-error",
                        dump_file,
                        timeout_seconds=60,
                    )
                environment["TEST_DATABASE_URL"] = (
                    f"postgresql://startunnel_admin:startunnel_admin@127.0.0.1:{port}/{database}"
                )
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--phase",
                        phase,
                        "--snapshot",
                        str(snapshot),
                    ],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    timeout=120,
                )
        print(
            json.dumps(
                {
                    "status": "passed",
                    "project": project_name,
                    "database": "PostgreSQL restored initial baseline",
                    "checks": [
                        "password and account ID",
                        "agent digest and authentication",
                        "original glyph address storage and access",
                        "address conversion refusal and transaction rollback",
                        "message tree and IDs",
                        "historical audit",
                        "private recovery key and admin authentication",
                        "new message write",
                    ],
                    "disposable_volumes_removed": True,
                    "limits": (
                        "No published predecessor or production snapshot claimed. "
                        "No reverse migration proof."
                    ),
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
