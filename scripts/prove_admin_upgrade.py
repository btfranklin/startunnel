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
SESSION_MIGRATION = ("tunnels", "0003_message_session")
PRE_BEACON_MIGRATION = ("tunnels", "0007_limit_cycle_root_checks_to_root_changes")


def targets_through(executor: Any, tunnel_migration: tuple[str, str]) -> list[tuple[str, str]]:
    return [node for node in executor.loader.graph.leaf_nodes() if node[0] != "tunnels"] + [
        tunnel_migration
    ]


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
    mention_model = apps.get_model("tunnels", "MessageMention")
    event_model = apps.get_model("tunnels", "TunnelEvent")
    checkpoint_model = apps.get_model("tunnels", "ActivityCheckpoint")
    idempotency_model = apps.get_model("tunnels", "IdempotencyRecord")
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
        digest_secret = secrets.token_bytes(32)
        address = encode_token(address_token)
        address_row = address_model.objects.create(
            tunnel=tunnel,
            generation=2,
            address_digest=hmac.digest(digest_secret, address_token, "sha256"),
        )
        retired_token = secrets.token_bytes(16)
        retired_address = encode_token(retired_token)
        address_model.objects.create(
            tunnel=tunnel,
            address_digest=hmac.digest(digest_secret, retired_token, "sha256"),
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
        cycle.content_bytes = sum(message.byte_count for message in messages)
        cycle.save()
        tunnel.next_event_position = 4
        tunnel.next_cycle_number = 2
        tunnel.save(update_fields=["next_event_position", "next_cycle_number"])
        for position, event_type, message in (
            (1, "cycle_started", messages[0]),
            (2, "message_posted", messages[0]),
            (3, "message_posted", messages[1]),
        ):
            event_model.objects.create(
                tunnel=tunnel,
                cycle=cycle,
                message=message,
                position=position,
                event_type=event_type,
            )
        mention_model.objects.create(
            message=messages[1], credential=credential, display_name=credential.name
        )
        checkpoint_model.objects.create(tunnel=tunnel, credential=credential, last_event_position=3)
        old_reply_key = "upgrade-proof-old-reply"
        idempotency_model.objects.create(
            credential=credential,
            operation="post_reply",
            key_digest=hmac.digest(
                settings.IDEMPOTENCY_SECRET.encode(), old_reply_key.encode(), "sha256"
            ),
            request_digest=hashlib.sha256(b"old reply request").digest(),
            response={
                "message_id": str(messages[1].pk),
                "cycle_id": str(cycle.pk),
                "sequence": 2,
                "event_position": 3,
            },
            resource_id=messages[1].pk,
            expires_at=timezone.now() + timedelta(hours=24),
        )
        audit = audit_model.objects.create(
            actor_user=user,
            action="human.created",
            target_type="user",
            target_id=user.pk,
            metadata={"historical": True},
        )
    admin_baseline = [
        ("accounts", "0002_admincredential_adminoperation"),
        ("agents", "0001_initial"),
        ("core", "0001_initial"),
        ("tunnels", "0001_initial"),
    ]
    executor = MigrationExecutor(connection)
    executor.migrate(admin_baseline)
    admin_apps = executor.loader.project_state(admin_baseline).apps
    admin_key_model = admin_apps.get_model("accounts", "AdminCredential")
    admin_operation_model = admin_apps.get_model("accounts", "AdminOperation")
    admin_key = "sta_" + base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    with transaction.atomic():
        admin_credential = admin_key_model.objects.create(
            owner_id=user.pk,
            name="upgrade-admin-key",
            display_prefix=admin_key[:12],
            key_digest=hmac.digest(settings.API_KEY_PEPPER.encode(), admin_key.encode(), "sha256"),
        )
        kept_operation = admin_operation_model.objects.create(
            owner_id=user.pk,
            operation="keys.create",
            key_digest=hashlib.sha256(b"keep admin receipt").digest(),
            request_digest=hashlib.sha256(b"keep admin request").digest(),
            derivation_nonce=secrets.token_bytes(32),
            response={"resource": {"id": str(admin_credential.pk)}},
            expires_at=timezone.now() + timedelta(hours=24),
        )
        removed_operation = admin_operation_model.objects.create(
            owner_id=user.pk,
            operation="tunnels.rotate",
            key_digest=hashlib.sha256(b"remove tunnel receipt").digest(),
            request_digest=hashlib.sha256(b"remove tunnel request").digest(),
            derivation_nonce=secrets.token_bytes(32),
            secret_resource_id=address_row.pk,
            response={"resource": {"id": str(tunnel.pk)}},
            expires_at=timezone.now() + timedelta(hours=24),
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
                "old_addresses": [address, retired_address],
                "admin_key": admin_key,
                "admin_credential_id": str(admin_credential.pk),
                "kept_admin_operation_id": str(kept_operation.pk),
                "removed_admin_operation_id": str(removed_operation.pk),
                "audit_id": str(audit.pk),
                "historical_tunnel_id": str(tunnel.pk),
                "historical_cycle_id": str(cycle.pk),
                "historical_root_id": str(messages[0].pk),
                "historical_reply_id": str(messages[1].pk),
                "historical_parent_id": str(messages[1].parent_id),
            }
        ),
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)
    connection.close()


def session_preserved(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    executor = MigrationExecutor(connection)
    targets = targets_through(executor, SESSION_MIGRATION)
    executor.migrate(targets)
    apps = executor.loader.project_state(targets).apps
    message_model = apps.get_model("tunnels", "Message")
    root = message_model.objects.get(pk=expected["historical_root_id"])
    reply = message_model.objects.get(pk=expected["historical_reply_id"])
    assert str(root.tunnel_id) == str(reply.tunnel_id) == expected["historical_tunnel_id"]
    assert str(root.cycle_id) == str(reply.cycle_id) == expected["historical_cycle_id"]
    assert root.parent_id is None and str(reply.parent_id) == expected["historical_parent_id"]
    assert reply.parent_id == root.pk
    assert root.session is None and reply.session is None
    connection.close()


def upgraded(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection, transaction
    from django.db.migrations.executor import MigrationExecutor

    from accounts.admin_access import authenticate_admin_key
    from accounts.admin_services import list_admin_resources
    from accounts.models import AdminCredential, AdminOperation, User
    from agents.models import AgentCredential
    from agents.services import authenticate_key
    from tunnels.access import resolve_tunnel
    from tunnels.cycles import create_tunnel
    from tunnels.errors import TunnelUnavailable
    from tunnels.models import (
        ActivityCheckpoint,
        AuditEvent,
        Cycle,
        IdempotencyRecord,
        Message,
        MessageMention,
        Tunnel,
        TunnelAddress,
        TunnelEvent,
    )

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    executor = MigrationExecutor(connection)
    assert ("tunnels", "0004_store_glyph_addresses") not in executor.loader.applied_migrations
    targets = targets_through(executor, PRE_BEACON_MIGRATION)
    executor.migrate(targets)

    user = User.objects.get(pk=expected["user_id"])
    assert user.password == expected["password_hash"] and user.check_password(expected["password"])
    credential = AgentCredential.objects.get(pk=expected["credential_id"])
    assert bytes(credential.key_digest).hex() == expected["key_digest"]
    assert authenticate_key(expected["agent_key"]).pk == credential.pk
    admin_credential = AdminCredential.objects.get(pk=expected["admin_credential_id"])
    assert authenticate_admin_key(expected["admin_key"]).pk == admin_credential.pk
    assert AdminOperation.objects.filter(pk=expected["kept_admin_operation_id"]).exists()
    assert not AdminOperation.objects.filter(pk=expected["removed_admin_operation_id"]).exists()

    for model in (
        Tunnel,
        TunnelAddress,
        Cycle,
        Message,
        MessageMention,
        TunnelEvent,
        ActivityCheckpoint,
        IdempotencyRecord,
    ):
        assert model.objects.count() == 0, model.__name__
    for old_address in expected["old_addresses"]:
        try:
            resolve_tunnel(credential=credential, address=old_address)
        except TunnelUnavailable:
            pass
        else:
            raise AssertionError("An old glyph address remained available after the reset.")

    audit = AuditEvent.objects.get(pk=expected["audit_id"])
    assert audit.action == "human.created" and audit.metadata == {"historical": True}
    assert audit.actor_user_id == user.pk and audit.actor_admin_credential_id is None
    assert audit.channel == ""

    created = create_tunnel(
        credential=credential,
        idempotency_key="upgrade-proof-new-tunnel",
        root_content={"type": "text", "text": "Fresh root"},
        root_session="proof-root-session",
    )
    inspected = list_admin_resources("tunnels", actor=user)["items"]
    assert len(inspected) == 1 and inspected[0]["address"] == created.address
    assert resolve_tunnel(credential=credential, address=created.address).pk == created.tunnel.pk
    historical_apps = executor.loader.project_state(targets).apps
    historical_cycle = historical_apps.get_model("tunnels", "Cycle")
    historical_message = historical_apps.get_model("tunnels", "Message")
    historical_event = historical_apps.get_model("tunnels", "TunnelEvent")
    historical_tunnel = historical_apps.get_model("tunnels", "Tunnel")
    reply_text = "Fresh reply"
    with transaction.atomic():
        historical_cycle.objects.filter(pk=created.cycle.pk).update(
            next_sequence=3,
            message_count=2,
            content_bytes=created.root.byte_count + len(reply_text.encode()),
        )
        historical_tunnel.objects.filter(pk=created.tunnel.pk).update(next_event_position=4)
        reply = historical_message.objects.create(
            tunnel_id=created.tunnel.pk,
            cycle_id=created.cycle.pk,
            parent_id=created.root.pk,
            sender_id=credential.pk,
            sender_name=credential.name,
            session="proof-reply-session",
            sequence=2,
            depth=1,
            payload_type="text",
            text_payload=reply_text,
            byte_count=len(reply_text.encode()),
        )
        historical_event.objects.create(
            tunnel_id=created.tunnel.pk,
            cycle_id=created.cycle.pk,
            message_id=reply.pk,
            position=3,
            event_type="message_posted",
        )
    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=created.tunnel, credential=credential, last_event_position=3
    )
    assert reply.parent_id == created.root.pk and reply.session == "proof-reply-session"
    assert Message.objects.filter(cycle=created.cycle).count() == 2

    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    assert ("tunnels", "0004_store_glyph_addresses") in executor.loader.applied_migrations
    assert Tunnel.objects.count() == 1
    assert TunnelAddress.objects.get(tunnel=created.tunnel).address == created.address
    assert Message.objects.filter(cycle=created.cycle).count() == 2
    assert Message.objects.get(pk=reply.pk).text_payload == "Fresh reply"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot.update(
        fresh_tunnel_id=str(created.tunnel.pk),
        fresh_cycle_id=str(created.cycle.pk),
        fresh_root_id=str(created.root.pk),
        fresh_reply_id=str(reply.pk),
        fresh_reply_parent_id=str(reply.parent_id),
        fresh_address=created.address,
        fresh_checkpoint_id=str(checkpoint.pk),
        fresh_checkpoint_position=checkpoint.last_event_position,
    )

    recovery_path = snapshot_path.parent / "recovery.key"
    call_command("recover_instance_admin", user.username, key_file=str(recovery_path), verbosity=0)
    assert recovery_path.stat().st_mode & 0o777 == 0o600
    recovered = authenticate_admin_key(recovery_path.read_text().strip())
    assert recovered.owner_id == user.pk
    assert AuditEvent.objects.filter(action="admin.recovered", channel="server").exists()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    snapshot_path.chmod(0o600)
    connection.close()


def beacon_upgraded(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.test import Client
    from django.utils import timezone

    from accounts.admin_access import authenticate_admin_key
    from accounts.models import AdminCredential, AdminOperation, User
    from agents.models import AgentCredential
    from tunnels.access import resolve_tunnel
    from tunnels.beacons import publish_beacon, read_board
    from tunnels.models import (
        ActivityCheckpoint,
        AuditEvent,
        Beacon,
        Cycle,
        Message,
        Tunnel,
        TunnelAddress,
        TunnelEvent,
    )

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    executor = MigrationExecutor(connection)
    assert PRE_BEACON_MIGRATION in executor.loader.applied_migrations
    executor.migrate(executor.loader.graph.leaf_nodes())

    user = User.objects.get(pk=expected["user_id"])
    credential = AgentCredential.objects.get(pk=expected["credential_id"])
    tunnel = Tunnel.objects.get(pk=expected["fresh_tunnel_id"])
    cycle = Cycle.objects.get(pk=expected["fresh_cycle_id"])
    root = Message.objects.get(pk=expected["fresh_root_id"])
    reply = Message.objects.get(pk=expected["fresh_reply_id"])
    assert tunnel.id == cycle.tunnel_id == root.tunnel_id == reply.tunnel_id
    assert cycle.root_message_id == root.pk and root.parent_id is None
    assert str(reply.parent_id) == expected["fresh_reply_parent_id"] == str(root.pk)
    assert root.session == "proof-root-session"
    assert reply.session == "proof-reply-session"
    assert root.text_payload == "Fresh root" and reply.text_payload == "Fresh reply"
    assert root.cycle_id == reply.cycle_id == cycle.pk
    assert not Beacon.objects.exists()
    assert TunnelAddress.objects.get(tunnel=tunnel).address == expected["fresh_address"]
    assert resolve_tunnel(credential=credential, address=expected["fresh_address"]).pk == tunnel.pk
    checkpoint = ActivityCheckpoint.objects.get(pk=expected["fresh_checkpoint_id"])
    assert checkpoint.tunnel_id == tunnel.pk
    assert checkpoint.last_event_position == expected["fresh_checkpoint_position"]
    audit = AuditEvent.objects.get(pk=expected["audit_id"])
    assert audit.actor_user_id == user.pk and audit.action == "human.created"
    admin_credential = AdminCredential.objects.get(pk=expected["admin_credential_id"])
    assert authenticate_admin_key(expected["admin_key"]).pk == admin_credential.pk
    assert AdminOperation.objects.filter(pk=expected["kept_admin_operation_id"]).exists()
    assert (
        AgentCredential.objects.get(pk=expected["credential_id"]).key_digest.hex()
        == expected["key_digest"]
    )

    result, replay = publish_beacon(
        credential=credential,
        idempotency_key="upgrade-proof-beacon",
        address=expected["fresh_address"],
        text="Beacon migration proof",
    )
    assert replay is False
    beacon = Beacon.objects.get(pk=result["id"])
    assert beacon.cycle_id == cycle.pk and beacon.publisher_id == credential.pk
    assert TunnelEvent.objects.filter(
        tunnel=tunnel, event_type=TunnelEvent.Type.BEACON_LIT
    ).exists()

    client = Client(HTTP_HOST="localhost")
    original_headers = {"Authorization": "Bearer " + expected["admin_key"]}
    assert client.get("/api/v1/admin/me", headers=original_headers).status_code == 200
    AdminCredential.objects.filter(owner=user).update(revoked_at=timezone.now())
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert client.get("/api/v1/admin/me", headers=original_headers).status_code == 401

    recovery_path = snapshot_path.parent / "beacon-recovery.key"
    call_command(
        "recover_instance_admin",
        user.username,
        key_file=str(recovery_path),
        key_name="beacon-recovery",
        verbosity=0,
    )
    assert recovery_path.stat().st_mode & 0o777 == 0o600
    recovery_key = recovery_path.read_text().strip()
    recovered = authenticate_admin_key(recovery_key)
    headers = {"Authorization": "Bearer " + recovery_key}
    inspected = client.get(f"/api/v1/admin/beacons/{beacon.pk}", headers=headers)
    assert inspected.status_code == 200 and inspected.json()["id"] == str(beacon.pk)
    body = json.dumps({"beacon_id": str(beacon.pk)})
    headers["Idempotency-Key"] = "upgrade-recovered-beacon-withdraw"
    withdrawn = client.post(
        "/api/v1/admin/beacons/withdraw",
        data=body,
        content_type="application/json",
        headers=headers,
    )
    assert withdrawn.status_code == 200
    receipt = withdrawn.json()
    assert receipt["resource"]["id"] == str(beacon.pk)
    assert receipt["resource"]["end_reason"] == Beacon.EndReason.WITHDRAWN
    retry = client.post(
        "/api/v1/admin/beacons/withdraw",
        data=body,
        content_type="application/json",
        headers=headers,
    )
    assert retry.status_code == 200 and retry.json() == receipt
    assert AuditEvent.objects.filter(
        action="beacon.ended", actor_admin_credential=recovered, channel="admin_api"
    ).exists()
    assert read_board(credential=credential).items == ()
    assert resolve_tunnel(credential=credential, address=expected["fresh_address"]).pk == tunnel.pk
    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == expected["fresh_checkpoint_position"]
    assert Message.objects.filter(cycle=cycle).count() == 2
    connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase", choices=("baseline", "session-preserved", "upgrade", "beacon-upgrade")
    )
    parser.add_argument("--snapshot", type=Path)
    arguments = parser.parse_args()
    if arguments.phase:
        if arguments.snapshot is None:
            parser.error("--snapshot is required with --phase")
        phases = {
            "baseline": baseline,
            "session-preserved": session_preserved,
            "upgrade": upgraded,
            "beacon-upgrade": beacon_upgraded,
        }
        phases[arguments.phase](arguments.snapshot)
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
            for database in (
                "admin_upgrade_source",
                "admin_upgrade_restored",
                "admin_upgrade_beacon_restored",
            ):
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
            for phase, source_database, target_database in (
                ("baseline", None, "admin_upgrade_source"),
                ("session-preserved", "admin_upgrade_source", "admin_upgrade_restored"),
                ("upgrade", None, "admin_upgrade_restored"),
                (
                    "beacon-upgrade",
                    "admin_upgrade_restored",
                    "admin_upgrade_beacon_restored",
                ),
            ):
                if source_database is not None:
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
                        "Dump restored migration baseline",
                        "exec",
                        "-T",
                        "postgres",
                        "pg_dump",
                        "--username",
                        "startunnel_admin",
                        "--format=custom",
                        "--file",
                        dump_file,
                        source_database,
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
                        f"--dbname={target_database}",
                        "--exit-on-error",
                        dump_file,
                        timeout_seconds=60,
                    )
                environment["TEST_DATABASE_URL"] = (
                    f"postgresql://startunnel_admin:startunnel_admin@127.0.0.1:{port}/{target_database}"
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
                        "one-time tunnel data reset",
                        "old glyph addresses unavailable",
                        "new tunnel and reply remain after a repeat migration",
                        "message UUIDs and ancestry survive session migration",
                        "fresh tunnel, sessions, credentials, audit, and checkpoint survive "
                        "Beacon migration",
                        "Beacon publication works after the forward upgrade",
                        "recovered admin withdraws Beacon with an exact receipt retry",
                        "Beacon withdrawal preserves the known address, messages, and checkpoint",
                        "admin credentials and non-tunnel receipts retained",
                        "tunnel admin receipts removed",
                        "historical audit",
                        "private recovery key and admin authentication",
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
