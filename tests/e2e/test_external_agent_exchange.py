"""Use real HTTP and PostgreSQL for the deterministic tunnel proof."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.getenv("STARTUNNEL_EXTERNAL_E2E") == "1"
pytestmark = pytest.mark.skipif(not ENABLED, reason="Use the deterministic agent profile.")


def _fixture_path() -> Path:
    path = Path("/tmp") / f"startunnel-deterministic-{uuid4().hex}.env"
    assert not path.exists()
    return path


def _read_environment(path: Path) -> dict[str, str]:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    values = dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line
    )
    assert {
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
        "STARTUNNEL_SENDER_ID",
        "STARTUNNEL_RECEIVER_ID",
    } <= values.keys()
    return values


def _headers(key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {key}"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def test_external_agent_exchange() -> None:
    required = {"DATABASE_URL", "STARTUNNEL_API_KEY_PEPPER", "STARTUNNEL_BASE_URL"}
    assert not (required - os.environ.keys())
    fixture = _fixture_path()
    values: dict[str, str] = {}
    try:
        environment = os.environ.copy()
        environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
        environment["STARTUNNEL_LIVE_AGENT_TESTS"] = "1"
        provisioned = subprocess.run(
            [sys.executable, "manage.py", "provision_live_agent_proof", "--output", str(fixture)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert provisioned.returncode == 0, "Deterministic fixture provisioning failed."
        values = _read_environment(fixture)
        sender_key = values["STARTUNNEL_SENDER_KEY"]
        receiver_key = values["STARTUNNEL_RECEIVER_KEY"]

        with httpx.Client(base_url=os.environ["STARTUNNEL_BASE_URL"], timeout=30) as client:
            created = client.post(
                "/api/v1/tunnels",
                headers=_headers(sender_key, f"external-create-{uuid4().hex}"),
                json={
                    "label": "External tunnel proof",
                    "cycle": {
                        "label": "Stable tree proof",
                        "root": {"content": {"type": "json", "value": {"action": "review"}}},
                    },
                },
            )
            assert created.status_code == 201
            body = created.json()
            tunnel_id = str(body["tunnel"]["id"])
            cycle_id = str(body["cycle"]["id"])
            root_id = str(body["cycle"]["root"]["id"])
            address = str(body["tunnel"]["address"])

            replied = client.post(
                "/api/v1/messages",
                headers=_headers(receiver_key, f"external-reply-{uuid4().hex}"),
                json={
                    "address": address,
                    "parent_id": root_id,
                    "content": {"type": "text", "text": "Review complete."},
                },
            )
            assert replied.status_code == 201
            reply_id = str(replied.json()["message"]["id"])

            tree = client.post(
                "/api/v1/tree",
                headers=_headers(receiver_key),
                json={"address": address, "cycle_id": cycle_id},
            )
            assert tree.status_code == 200
            assert [str(node["id"]) for node in tree.json()["nodes"]] == [root_id, reply_id]

            closed = client.post(
                "/api/v1/cycles/close",
                headers=_headers(sender_key, f"external-close-{uuid4().hex}"),
                json={"address": address, "expected_cycle_id": cycle_id},
            )
            assert closed.status_code == 204

        with (
            psycopg.connect(os.environ["DATABASE_URL"]) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT creator_id::text, state FROM tunnels_tunnel WHERE id = %s",
                (tunnel_id,),
            )
            assert cursor.fetchone() == (values["STARTUNNEL_SENDER_ID"], "dormant")
            cursor.execute(
                "SELECT state, final_sequence, message_count, delete_after "
                "FROM tunnels_cycle WHERE id = %s",
                (cycle_id,),
            )
            cycle = cursor.fetchone()
            assert cycle is not None
            assert cycle[:3] == ("closed", 2, 2)
            assert cycle[3] is not None
            cursor.execute(
                "UPDATE tunnels_cycle SET delete_after = now() - interval '1 second' WHERE id = %s",
                (cycle_id,),
            )
            cursor.execute("SELECT pg_notify('startunnel_maintenance', 'cycle_due')")

        deadline = time.monotonic() + 30
        deletion_state: tuple[str, int] | None = None
        while time.monotonic() < deadline:
            with (
                psycopg.connect(os.environ["DATABASE_URL"]) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT c.state, count(m.id)::integer FROM tunnels_cycle c "
                    "LEFT JOIN tunnels_message m ON m.cycle_id = c.id "
                    "WHERE c.id = %s GROUP BY c.state",
                    (cycle_id,),
                )
                deletion_state = cursor.fetchone()
            if deletion_state == ("deleted", 0):
                break
            time.sleep(0.25)
        assert deletion_state == ("deleted", 0)

        with httpx.Client(base_url=os.environ["STARTUNNEL_BASE_URL"], timeout=30) as client:
            unavailable = client.post(
                "/api/v1/tree",
                headers=_headers(receiver_key),
                json={"address": address, "cycle_id": cycle_id},
            )
            assert unavailable.status_code == 404
            assert unavailable.json()["error"]["code"] == "cycle_unavailable"
    finally:
        fixture.unlink(missing_ok=True)
        ids = [
            values[name]
            for name in ("STARTUNNEL_SENDER_ID", "STARTUNNEL_RECEIVER_ID")
            if name in values
        ]
        if ids and os.getenv("DATABASE_URL"):
            with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
                connection.execute(
                    "UPDATE agents_agentcredential SET revoked_at = now() WHERE id = ANY(%s)",
                    (ids,),
                )
