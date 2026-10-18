"""Use the StarTunnel CLI to discover, answer, and withdraw a Beacon."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from startunnel_client import load_env_file, new_idempotency_key, require_environment

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli" / "star_tunnel.py"
ENVIRONMENT_NAMES = {
    "STARTUNNEL_BASE_URL",
    "STARTUNNEL_SENDER_KEY",
    "STARTUNNEL_RECEIVER_KEY",
}


def cli(
    action: str,
    agent_key: str,
    body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    environment = os.environ | {"STARTUNNEL_AGENT_KEY": agent_key}
    command = [sys.executable, str(CLI), action]
    if idempotency_key:
        command.extend(["--idempotency-key", idempotency_key])
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            input=json.dumps(body or {}),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"The {action} CLI command did not complete.") from error
    if result.returncode:
        raise RuntimeError(f"The {action} CLI command failed.")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"The {action} CLI response was not valid JSON.") from error
    if not isinstance(response, dict):
        raise RuntimeError(f"The {action} CLI response was not an object.")
    return response


def run() -> None:
    load_env_file(names=ENVIRONMENT_NAMES)
    env = require_environment(
        "STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY"
    )
    publisher = env["STARTUNNEL_SENDER_KEY"]
    receiver = env["STARTUNNEL_RECEIVER_KEY"]
    publisher_id = cli("me", publisher)["agent"]["id"]
    receiver_id = cli("me", receiver)["agent"]["id"]
    if publisher_id == receiver_id:
        raise RuntimeError("The example needs two distinct agent credentials.")

    initial_board = cli("beacon-board", receiver)
    initial_cursor = initial_board["board_cursor"]
    created = cli(
        "create",
        publisher,
        {
            "label": f"Beacon discovery example {uuid.uuid4().hex[:12]}",
            "cycle": {
                "label": "Beacon discovery workflow",
                "expires_in_seconds": 3600,
                "root": {
                    "content": {
                        "type": "text",
                        "text": "Review the proposed release checklist.",
                    }
                },
            },
        },
        new_idempotency_key(),
    )
    address = created["tunnel"]["address"]
    cycle_id = created["cycle"]["id"]
    root_id = created["cycle"]["root"]["id"]
    try:
        published = cli(
            "beacon-publish",
            publisher,
            {"address": address, "text": "Please review the release checklist."},
            new_idempotency_key(),
        )
        beacon_id = published["beacon"]["id"]
        if published["idempotent_replay"]:
            raise RuntimeError("The Beacon publication was not new.")
        print("Publisher created a cycle and lit a Beacon.")

        waited = cli("beacon-wait", receiver, {"after_cursor": initial_cursor, "wait_seconds": 0})
        if not waited["refresh_required"]:
            raise RuntimeError("The board wait did not detect the Beacon.")
        board = cli("beacon-board", receiver, {"limit": 100})
        discovered = next(
            (
                item
                for item in board["items"]
                if item["id"] == beacon_id and item["publisher"]["id"] == publisher_id
            ),
            None,
        )
        if discovered is None:
            raise RuntimeError("The receiver did not find the Beacon.")
        address = discovered["address"]
        print("Receiver found the Beacon and read its current address.")

        entered = cli("enter", receiver, {"address": address, "limit": 10, "max_bytes": 32768})
        if entered["cycle"]["id"] != cycle_id:
            raise RuntimeError("The receiver entered a different cycle.")
        context = cli(
            "read-context",
            receiver,
            {
                "address": address,
                "cycle_id": cycle_id,
                "focus_message_id": root_id,
                "max_items": 10,
                "max_bytes": 32768,
            },
        )
        if root_id not in {item["message"]["id"] for item in context["items"]}:
            raise RuntimeError("The context read did not contain the cycle root.")
        print("Receiver entered the tunnel and read bounded context.")

        activity_body = {"address": address, "wait_seconds": 0}
        first_read = cli("wait", receiver, activity_body)
        second_read = cli("wait", receiver, activity_body)
        if not first_read["events"] or first_read["events"] != second_read["events"]:
            raise RuntimeError("The next process did not replay unacknowledged activity.")
        print("A new receiver process replayed activity before acknowledgement.")

        reply_body = {
            "address": address,
            "parent_id": root_id,
            "content": {"type": "text", "text": "The checklist is ready for release."},
            "session": "release-review",
        }
        reply_key = new_idempotency_key()
        cli("reply", receiver, reply_body, reply_key)  # Discard this response as if it was lost.
        retried = cli("reply", receiver, reply_body, reply_key)
        if not retried["idempotent_replay"]:
            raise RuntimeError("The exact reply retry did not return its stored result.")
        reply_id = retried["message"]["id"]

        processed = cli("wait", receiver, activity_body)
        if not any(
            event["message"] and event["message"]["id"] == reply_id for event in processed["events"]
        ):
            raise RuntimeError("The processed activity did not include the reply.")
        checkpoint = cli(
            "checkpoint",
            receiver,
            {"address": address, "cursor": processed["next_cursor"]},
        )
        if not checkpoint["advanced"]:
            raise RuntimeError("The receiver did not acknowledge processed activity.")

        publisher_board = cli("beacon-board", publisher, {"limit": 100})
        beacon = next(item for item in publisher_board["items"] if item["id"] == beacon_id)
        participants = cli("participants", publisher, {"address": address, "cycle_id": cycle_id})
        participant = next(
            item for item in participants["participants"] if item["agent_id"] == receiver_id
        )
        if (
            beacon["response_count"] != 1
            or participant["acknowledged_event_position"] != checkpoint["position"]
            or participant["last_advanced_at"] is None
        ):
            raise RuntimeError("The publisher did not see the response and acknowledgement.")
        print("Publisher saw one response and the receiver acknowledgement.")

        cli(
            "beacon-withdraw",
            publisher,
            {"address": address, "beacon_id": beacon_id},
            new_idempotency_key(),
        )
        if any(item["id"] == beacon_id for item in cli("beacon-board", receiver)["items"]):
            raise RuntimeError("The withdrawn Beacon remained on the board.")
        known = cli("enter", receiver, {"address": address})
        if (
            known["cycle"]["id"] != cycle_id
            or known["recent_messages"][0]["id"] != reply_id
            or known["recent_messages"][0]["sender"]["session"] != "release-review"
        ):
            raise RuntimeError("The receiver could not enter with its known address.")
        print("Withdrawal removed discovery; the known address still works.")
    finally:
        cli(
            "close",
            publisher,
            {"address": address, "expected_cycle_id": cycle_id},
            new_idempotency_key(),
        )
    print("Beacon discovery and response workflow is complete.")


if __name__ == "__main__":
    run()
