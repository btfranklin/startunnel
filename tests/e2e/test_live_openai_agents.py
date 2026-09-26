"""Run the optional OpenAI tree proof and verify PostgreSQL evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[2]
ENABLED = os.getenv("STARTUNNEL_LIVE_AGENT_TESTS") == "1"
SCENARIO_FIELDS = {
    "tunnel_id",
    "cycle_id",
    "root_message_id",
    "reply_message_id",
    "confirmation_message_id",
    "sender_credential_id",
    "receiver_credential_id",
    "http_events",
    "tool_calls",
    "structured_results_validated",
}
EXPECTED_TOOL_CALLS = [
    "create_tunnel_with_root",
    "read_root_and_reply",
    "read_reply_and_confirm",
]
EXPECTED_HTTP_EVENTS = {
    "tunnel_and_root_created",
    "receiver_read_root",
    "receiver_reply_posted",
    "sender_read_reply",
    "sender_confirmation_posted",
    "cycle_closed",
    "retained_tree_read",
}

pytestmark = [
    pytest.mark.live_agents,
    pytest.mark.skipif(
        not ENABLED,
        reason="Set STARTUNNEL_LIVE_AGENT_TESTS=1 to spend OpenAI API credit.",
    ),
]


def _unique_tmpfs_path(label: str, suffix: str) -> Path:
    path = Path("/tmp") / f"startunnel-{label}-{uuid4().hex}{suffix}"
    assert not path.exists()
    return path


def _load_safe_evidence(path: Path) -> dict[str, Any]:
    raw: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    assert set(raw) == {"scenarios"}
    raw_scenarios = raw["scenarios"]
    assert isinstance(raw_scenarios, list)
    assert len(raw_scenarios) == 1
    scenario = raw_scenarios[0]
    assert isinstance(scenario, dict)
    assert set(scenario) == SCENARIO_FIELDS
    assert scenario["structured_results_validated"] is True
    assert scenario["tool_calls"] == EXPECTED_TOOL_CALLS
    assert isinstance(scenario["http_events"], list)
    assert set(scenario["http_events"]) == EXPECTED_HTTP_EVENTS
    for field in (
        "tunnel_id",
        "cycle_id",
        "root_message_id",
        "reply_message_id",
        "confirmation_message_id",
        "sender_credential_id",
        "receiver_credential_id",
    ):
        assert isinstance(scenario[field], str) and scenario[field]
    return scenario


def _assert_database_evidence(
    database_url: str,
    scenario: dict[str, Any],
) -> None:
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT creator_id::text, state FROM tunnels_tunnel WHERE id = %s",
            (scenario["tunnel_id"],),
        )
        assert cursor.fetchone() == (scenario["sender_credential_id"], "dormant")

        cursor.execute(
            """
            SELECT state, final_sequence, message_count, delete_after
            FROM tunnels_cycle
            WHERE id = %s AND tunnel_id = %s
            """,
            (scenario["cycle_id"], scenario["tunnel_id"]),
        )
        cycle = cursor.fetchone()
        assert cycle is not None
        assert cycle[0] == "closed"
        assert cycle[1] == 3
        assert cycle[2] == 3
        assert cycle[3] is not None

        cursor.execute(
            """
            SELECT id::text, sender_id::text, parent_id::text, sequence,
                   text_payload, json_payload
            FROM tunnels_message
            WHERE cycle_id = %s
            ORDER BY sequence
            """,
            (scenario["cycle_id"],),
        )
        messages = cursor.fetchall()
        assert [message[0] for message in messages] == [
            scenario["root_message_id"],
            scenario["reply_message_id"],
            scenario["confirmation_message_id"],
        ]
        assert [message[1] for message in messages] == [
            scenario["sender_credential_id"],
            scenario["receiver_credential_id"],
            scenario["sender_credential_id"],
        ]
        assert [message[2] for message in messages] == [
            None,
            scenario["root_message_id"],
            scenario["reply_message_id"],
        ]
        assert [message[3] for message in messages] == [1, 2, 3]
        assert all(message[4] is not None or message[5] is not None for message in messages)


def test_live_openai_agents_complete_tunnel_tree_exchange() -> None:
    required = {
        "DATABASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "STARTUNNEL_API_KEY_PEPPER",
        "STARTUNNEL_BASE_URL",
    }
    missing = sorted(name for name in required if not os.getenv(name))
    assert not missing, "Missing live-agent setting names: " + ", ".join(missing)

    credentials_path = _unique_tmpfs_path("live-credentials", ".env")
    evidence_path = _unique_tmpfs_path("live-evidence", ".json")
    try:
        provision_environment = os.environ.copy()
        provision_environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
        provision_environment["STARTUNNEL_LIVE_AGENT_TESTS"] = "1"
        provisioned = subprocess.run(
            [
                sys.executable,
                "manage.py",
                "provision_live_agent_proof",
                "--output",
                str(credentials_path),
            ],
            cwd=ROOT,
            env=provision_environment,
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert provisioned.returncode == 0, provisioned.stdout + provisioned.stderr
        assert credentials_path.is_file()
        assert credentials_path.stat().st_mode & 0o777 == 0o600

        agent_environment = os.environ.copy()
        agent_environment.pop("PYTHONPATH", None)
        agent_environment["STARTUNNEL_LIVE_AGENT_TESTS"] = "1"
        agent_environment["STARTUNNEL_LIVE_CREDENTIALS_FILE"] = str(credentials_path)
        agent_environment["STARTUNNEL_LIVE_EVIDENCE_PATH"] = str(evidence_path)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "examples.openai_agents.run_exchange",
            ],
            cwd=ROOT,
            env=agent_environment,
            text=True,
            capture_output=True,
            timeout=900,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Tunnel proof" in result.stdout
        assert "Public API evidence is complete" in result.stdout
        assert "Agent prose was not used as proof" in result.stdout
        assert evidence_path.stat().st_mode & 0o777 == 0o600

        scenario = _load_safe_evidence(evidence_path)
        _assert_database_evidence(os.environ["DATABASE_URL"], scenario)
    finally:
        credentials_path.unlink(missing_ok=True)
        evidence_path.unlink(missing_ok=True)
