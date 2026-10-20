"""Prove a restored v0.2.0 database upgrades and recovers on current source."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "v0.2.0"


def published_record(path: Path) -> dict[str, str]:
    """Validate the published release record against its source tag."""

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.start_production import validate_image_reference

    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("version") != VERSION:
        raise ValueError(f"The published predecessor must be {VERSION}.")
    revision = subprocess.check_output(
        ["git", "rev-parse", f"{VERSION}^{{commit}}"], cwd=ROOT, text=True
    ).strip()
    if record.get("source_commit") != revision:
        raise ValueError(f"Published {VERSION} source differs from its tag.")
    image = validate_image_reference(record.get("image", ""))
    if record.get("platform") != "linux/amd64":
        raise ValueError(f"Published {VERSION} uses the linux/amd64 platform.")
    return {"version": VERSION, "source_commit": revision, "image": image}


def assert_candidate_identity(labels: dict[str, str], revision: str, version: str) -> None:
    """Require a candidate image built from this exact source and version."""

    if labels.get("org.opencontainers.image.revision") != revision:
        raise ValueError("Candidate image source differs from the current checkout.")
    if labels.get("org.opencontainers.image.version") != version:
        raise ValueError("Candidate image version differs from the current VERSION file.")


def seed(snapshot_path: Path) -> None:
    """Create account, credential, and message records with the published app."""

    import django

    django.setup()
    from django.contrib.auth.hashers import make_password
    from django.db import connection

    from accounts.admin_access import issue_admin_key
    from accounts.models import User
    from agents.services import create_credential
    from tunnels.cycles import create_tunnel

    password = "Proof-" + base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    user = User.objects.create(username="v020-upgrade-proof", password=make_password(password))
    admin, admin_key = issue_admin_key(owner=user, name="v020 upgrade proof")
    issued = create_credential(actor=user, name="v020 upgrade proof")
    created = create_tunnel(
        credential=issued.credential,
        idempotency_key="v020-upgrade-proof-root",
        root_content={"type": "text", "text": "Message retained across upgrade"},
    )
    from tunnels.messages import post_reply

    reply = post_reply(
        credential=issued.credential,
        address=created.address,
        idempotency_key="v020-upgrade-proof-reply",
        parent_id=created.root.pk,
        content={"type": "text", "text": "Reply retained across upgrade"},
    )
    snapshot_path.write_text(
        json.dumps(
            {
                "user_id": str(user.pk),
                "username": user.username,
                "password": password,
                "agent_key": issued.key,
                "agent_id": str(issued.credential.pk),
                "admin_key": admin_key,
                "admin_id": str(admin.pk),
                "tunnel_id": str(created.tunnel.pk),
                "address": created.address,
                "root_id": str(created.root.pk),
                "reply_id": str(reply.message.pk),
            }
        ),
        encoding="utf-8",
    )
    snapshot_path.chmod(0o600)
    connection.close()


def _assert_published_data(snapshot_path: Path) -> None:
    import django

    django.setup()
    from django.test import Client

    from accounts.admin_access import authenticate_admin_key
    from accounts.models import User
    from agents.services import authenticate_key
    from tunnels.models import Message, Tunnel

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    user = User.objects.get(pk=expected["user_id"])
    assert user.check_password(expected["password"])
    client = Client()
    response = client.post(
        "/accounts/login/",
        {"username": expected["username"], "password": expected["password"]},
    )
    assert (
        response.status_code == 302 and client.session.get("_auth_user_id") == expected["user_id"]
    )
    assert str(authenticate_key(expected["agent_key"]).pk) == expected["agent_id"]
    assert str(authenticate_admin_key(expected["admin_key"]).pk) == expected["admin_id"]
    tunnel = Tunnel.objects.get(pk=expected["tunnel_id"])
    assert tunnel.addresses.filter(address=expected["address"], state="current").exists()
    root = Message.objects.get(pk=expected["root_id"])
    reply = Message.objects.get(pk=expected["reply_id"])
    assert root.text_payload == "Message retained across upgrade"
    assert reply.text_payload == "Reply retained across upgrade"
    assert root.parent_id is None and reply.parent_id == root.pk


def published_recovery(snapshot_path: Path) -> None:
    """Check login, key authentication, and retained messages on v0.2.0."""

    _assert_published_data(snapshot_path)


def current_upgrade(snapshot_path: Path) -> None:
    """Migrate the restored database and prove writes, maintenance, and recovery."""

    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connection
    from django.utils import timezone

    from accounts.admin_access import authenticate_admin_key
    from accounts.models import AdminCredential
    from agents.services import authenticate_key
    from core.maintenance import cleanup_once
    from tunnels.cycles import create_tunnel
    from tunnels.models import Cycle

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    call_command("migrate", interactive=False, verbosity=0)
    _assert_published_data(snapshot_path)
    credential = authenticate_key(expected["agent_key"])
    added = create_tunnel(
        credential=credential,
        idempotency_key="v030-upgrade-proof-write",
        root_content={"type": "text", "text": "New write after upgrade"},
    )
    assert added.root.text_payload == "New write after upgrade"
    due = create_tunnel(
        credential=credential,
        idempotency_key="v030-upgrade-proof-maintenance",
        root_content={"type": "text", "text": "Maintenance deadline"},
        expires_in_seconds=300,
    )
    stats = cleanup_once(now=timezone.now() + timedelta(seconds=301))
    due.cycle.refresh_from_db()
    assert due.cycle.state == Cycle.State.CLOSED and stats["cycles_closed"] >= 1

    AdminCredential.objects.filter(owner_id=expected["user_id"], revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )
    key_file = snapshot_path.parent / "recovered-admin.key"
    call_command(
        "recover_instance_admin", expected["username"], key_file=str(key_file), verbosity=0
    )
    assert key_file.stat().st_mode & 0o777 == 0o600
    recovered = authenticate_admin_key(key_file.read_text(encoding="utf-8").strip())
    assert str(recovered.owner_id) == expected["user_id"]
    _assert_published_data_without_admin_key(snapshot_path)
    key_file.unlink()
    connection.close()


def _assert_published_data_without_admin_key(snapshot_path: Path) -> None:
    """Check durable account and message data after admin-key recovery."""

    import django

    django.setup()
    from django.test import Client

    from accounts.models import User
    from agents.services import authenticate_key
    from tunnels.models import Message

    expected = json.loads(snapshot_path.read_text(encoding="utf-8"))
    user = User.objects.get(pk=expected["user_id"])
    client = Client()
    response = client.post(
        "/accounts/login/",
        {"username": expected["username"], "password": expected["password"]},
    )
    assert response.status_code == 302
    assert str(authenticate_key(expected["agent_key"]).pk) == expected["agent_id"]
    assert Message.objects.filter(
        pk=expected["root_id"], tunnel__creator_id=expected["agent_id"]
    ).exists()
    assert user.is_active
    assert Message.objects.filter(text_payload="New write after upgrade").exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--release-record", required=True, type=Path, help="Published v0.2.0 release.json"
    )
    parser.add_argument("--candidate-image", help="Optional immutable current candidate image")
    arguments = parser.parse_args()
    record = published_record(arguments.release_record)
    candidate_image = None
    if arguments.candidate_image:
        from scripts.start_production import validate_image_reference

        candidate_image = validate_image_reference(arguments.candidate_image)
    sys.path.insert(0, str(ROOT))
    from scripts.compose_isolation import IsolatedComposeProject, generated_project_name

    os.umask(0o077)
    project = generated_project_name("v020-upgrade")
    with tempfile.TemporaryDirectory(prefix="startunnel-v020-upgrade-") as directory:
        temporary = Path(directory).resolve()
        overlay = temporary / "published-runtime.yaml"
        user = f"{os.getuid()}:{os.getgid()}" if sys.platform == "linux" else "10001:10001"
        overlay.write_text(
            'services:\n  postgres:\n    ports: ["127.0.0.1::5432"]\n'
            "  published-proof:\n"
            f"    image: {record['image']}\n"
            "    platform: linux/amd64\n"
            f"    user: {json.dumps(user)}\n"
            "    entrypoint: [/app/.venv/bin/python]\n"
            "    read_only: true\n"
            "    tmpfs: [/tmp:mode=1777]\n"
            "    networks: [startunnel]\n"
            "    environment:\n"
            "      DJANGO_SETTINGS_MODULE: startunnel.settings.test\n"
            "      STARTUNNEL_ALLOWED_HOSTS: testserver,localhost,127.0.0.1,web\n"
            "      STARTUNNEL_API_KEY_PEPPER: upgrade-proof-api-pepper\n"
            "      STARTUNNEL_IDEMPOTENCY_SECRET: upgrade-proof-idempotency-secret\n"
            "      TEST_DATABASE_URL: postgresql://startunnel_admin:startunnel_admin@postgres:5432/v020_source\n"
            "    volumes:\n"
            f"      - {json.dumps(str(Path(__file__).resolve()) + ':/proof/run.py:ro')}\n"
            f"      - {json.dumps(str(temporary) + ':/proof-data')}\n",
            encoding="utf-8",
        )
        if candidate_image:
            with overlay.open("a", encoding="utf-8") as stream:
                stream.write(
                    "  candidate-proof:\n"
                    f"    image: {candidate_image}\n"
                    "    platform: linux/amd64\n"
                    f"    user: {json.dumps(user)}\n"
                    "    entrypoint: [/app/.venv/bin/python]\n"
                    "    read_only: true\n"
                    "    tmpfs: [/tmp:mode=1777]\n"
                    "    networks: [startunnel]\n"
                    "    environment:\n"
                    "      DJANGO_SETTINGS_MODULE: startunnel.settings.test\n"
                    "      STARTUNNEL_ALLOWED_HOSTS: testserver,localhost,127.0.0.1,web\n"
                    "      STARTUNNEL_API_KEY_PEPPER: upgrade-proof-api-pepper\n"
                    "      STARTUNNEL_IDEMPOTENCY_SECRET: upgrade-proof-idempotency-secret\n"
                    "      TEST_DATABASE_URL: postgresql://startunnel_admin:startunnel_admin@postgres:5432/v020_restored\n"
                    "    volumes:\n"
                    f"      - {json.dumps(str(Path(__file__).resolve()) + ':/proof/run.py:ro')}\n"
                    f"      - {json.dumps(str(temporary) + ':/proof-data')}\n"
                )

        class ProofProject(IsolatedComposeProject):
            def compose_files(self) -> tuple[str, ...]:
                return (*super().compose_files(), str(overlay))

        with ProofProject(project) as stack:
            stack.run("Start isolated PostgreSQL", "up", "--wait", "postgres", timeout_seconds=180)
            port = stack.capture("Local database port", "port", "postgres", "5432").rsplit(":", 1)[
                1
            ]
            for database in ("v020_source", "v020_restored"):
                stack.run(
                    "Create disposable database",
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
                STARTUNNEL_ALLOWED_HOSTS="testserver,localhost,127.0.0.1,web",
                STARTUNNEL_API_KEY_PEPPER="upgrade-proof-api-pepper",
                STARTUNNEL_IDEMPOTENCY_SECRET="upgrade-proof-idempotency-secret",
                PYTHONPATH=str(ROOT / "src"),
            )
            snapshot = temporary / "snapshot.json"
            stack.run("Pull published v0.2.0 image", "pull", "published-proof", timeout_seconds=300)
            if candidate_image:
                stack.run("Pull candidate image", "pull", "candidate-proof", timeout_seconds=300)
                candidate_labels = json.loads(
                    subprocess.check_output(
                        [
                            "docker",
                            "image",
                            "inspect",
                            candidate_image,
                            "--format",
                            "{{json .Config.Labels}}",
                        ],
                        env=stack.environment,
                        text=True,
                    )
                )
                current_revision = subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip()
                current_version = "v" + (ROOT / "VERSION").read_text(encoding="utf-8").strip()
                assert_candidate_identity(candidate_labels, current_revision, current_version)
            labels = json.loads(
                subprocess.check_output(
                    [
                        "docker",
                        "image",
                        "inspect",
                        record["image"],
                        "--format",
                        "{{json .Config.Labels}}",
                    ],
                    env=stack.environment,
                    text=True,
                )
            )
            assert labels.get("org.opencontainers.image.revision") == record["source_commit"]
            assert labels.get("org.opencontainers.image.version") == VERSION
            source_url = "postgresql://startunnel_admin:startunnel_admin@postgres:5432/v020_source"
            stack.run(
                "Migrate database with published v0.2.0 runtime",
                "run",
                "--rm",
                "--no-deps",
                "--env",
                "TEST_DATABASE_URL=postgresql://startunnel_admin:startunnel_admin@postgres:5432/v020_source",
                "published-proof",
                "/app/manage.py",
                "migrate",
                "--noinput",
                timeout_seconds=120,
            )
            stack.run(
                "Seed with published v0.2.0 runtime",
                "run",
                "--rm",
                "--no-deps",
                "--env",
                f"TEST_DATABASE_URL={source_url}",
                "published-proof",
                "/proof/run.py",
                "--phase",
                "seed",
                "--snapshot",
                "/proof-data/snapshot.json",
                timeout_seconds=120,
            )
            dump = "/tmp/v020-upgrade.dump"
            stack.run(
                "Dump v0.2.0 database",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "--username",
                "startunnel_admin",
                "--format=custom",
                "--file",
                dump,
                "v020_source",
                timeout_seconds=60,
            )
            stack.run(
                "Restore v0.2.0 database",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--username",
                "startunnel_admin",
                "--dbname=v020_restored",
                "--exit-on-error",
                dump,
                timeout_seconds=60,
            )
            restored_url = (
                "postgresql://startunnel_admin:startunnel_admin@postgres:5432/v020_restored"
            )
            stack.run(
                "Verify restored data with published runtime",
                "run",
                "--rm",
                "--no-deps",
                "--env",
                f"TEST_DATABASE_URL={restored_url}",
                "published-proof",
                "/proof/run.py",
                "--phase",
                "published-recovery",
                "--snapshot",
                "/proof-data/snapshot.json",
                timeout_seconds=120,
            )
            environment["TEST_DATABASE_URL"] = (
                f"postgresql://startunnel_admin:startunnel_admin@127.0.0.1:{port}/v020_restored"
            )
            if candidate_image:
                stack.run(
                    "Run upgrade proof on candidate image",
                    "run",
                    "--rm",
                    "--no-deps",
                    "--env",
                    f"TEST_DATABASE_URL={restored_url}",
                    "candidate-proof",
                    "/proof/run.py",
                    "--phase",
                    "current-upgrade",
                    "--snapshot",
                    "/proof-data/snapshot.json",
                    timeout_seconds=180,
                )
            else:
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--phase",
                        "current-upgrade",
                        "--snapshot",
                        str(snapshot),
                    ],
                    cwd=ROOT,
                    env=environment,
                    check=True,
                    timeout=180,
                )
    print(
        json.dumps(
            {
                "status": "passed",
                "published_baseline": record,
                "checks": [
                    "restored v0.2.0 account login",
                    "existing agent and admin authentication",
                    "retained message ancestry",
                    "current-source migrations",
                    "new tunnel write",
                    "maintenance closes due cycle",
                    "private admin recovery key",
                ],
                "disposable_volumes_removed": True,
            }
        )
    )
    return 0


def phase() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--phase", choices=("seed", "published-recovery", "current-upgrade"), required=True
    )
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    {"seed": seed, "published-recovery": published_recovery, "current-upgrade": current_upgrade}[
        args.phase
    ](args.snapshot)
    return 0


if __name__ == "__main__":
    if "--phase" in sys.argv:
        raise SystemExit(phase())
    raise SystemExit(main())
