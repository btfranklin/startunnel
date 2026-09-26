"""Prove safe tree-reader recovery after the web service restarts.

This worker runs in the isolated example-test container. It prints only fixed
coordination markers. It does not print credentials, addresses, identifiers,
cursors, or message content.
"""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess  # nosec B404
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from examples.python.startunnel_client import StarTunnelClient, StarTunnelError

WAITING_MARKER = "STARTUNNEL_ACTIVITY_READING"
INTERRUPTED_MARKER = "STARTUNNEL_ACTIVITY_INTERRUPTED_SAFE"
COMPLETE_MARKER = "STARTUNNEL_ACTIVITY_RECOVERY_COMPLETE"
FAILED_MARKER = "STARTUNNEL_ACTIVITY_PROOF_FAILED"
FAILURE_STAGE_PREFIX = "STARTUNNEL_ACTIVITY_FAILURE_STAGE_"
FIXTURE_NAMES = (
    "STARTUNNEL_SENDER_KEY",
    "STARTUNNEL_RECEIVER_KEY",
    "STARTUNNEL_SENDER_ID",
    "STARTUNNEL_RECEIVER_ID",
)


class ProofError(RuntimeError):
    """The bounded restart proof did not observe its required behavior."""


def _fixture_path() -> Path:
    return Path(tempfile.gettempdir()) / f"startunnel-activity-{uuid4().hex}.env"


def _read_fixture(path: Path) -> dict[str, str]:
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ProofError("The protected fixture has an invalid mode.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator:
            values[name] = value
    if any(not values.get(name) for name in FIXTURE_NAMES):
        raise ProofError("The protected fixture is incomplete.")
    return values


def _provision(path: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "startunnel.settings.development"
    environment["STARTUNNEL_LIVE_AGENT_TESTS"] = "1"
    result = subprocess.run(  # nosec B603
        [
            sys.executable,
            "manage.py",
            "provision_live_agent_proof",
            "--output",
            str(path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise ProofError("Fixture provisioning failed.")
    return _read_fixture(path)


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname != "web" or parsed.port != 8000:
        raise ProofError("The proof requires the isolated web service URL.")
    return value.rstrip("/")


def _interruption_is_safe(error: StarTunnelError) -> bool:
    return (error.status_code == 0 and error.code == "connection_failed") or (
        error.status_code == 503 and error.code == "dependency_unavailable"
    )


async def _wait_for_api(client: StarTunnelClient, *, timeout_seconds: float = 120) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        try:
            await client.me()
            return
        except StarTunnelError:
            await asyncio.sleep(0.25)
    raise ProofError("The public API did not recover inside the time limit.")


def _object(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProofError(f"The {description} response is not valid.")
    return value


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProofError(f"The {description} response is not valid.")
    return value


async def _run_public_proof(values: dict[str, str], base_url: str) -> None:
    create_key = f"activity-create-{uuid4().hex}"
    reply_key = f"activity-reply-{uuid4().hex}"
    close_key = f"activity-close-{uuid4().hex}"
    closed = False
    async with (
        StarTunnelClient(base_url, values["STARTUNNEL_SENDER_KEY"]) as sender,
        StarTunnelClient(base_url, values["STARTUNNEL_RECEIVER_KEY"]) as receiver,
    ):
        created = await sender.create_tunnel(
            label="Activity restart proof",
            cycle_label="Restart-safe tree",
            root_content={"type": "text", "text": "Verify tree recovery."},
            idempotency_key=create_key,
            expires_in_seconds=300,
        )
        tunnel = _object(created.get("tunnel"), "tunnel")
        cycle = _object(created.get("cycle"), "cycle")
        root = _object(cycle.get("root"), "root")
        address = _string(tunnel.get("address"), "address")
        cycle_id = _string(cycle.get("id"), "cycle ID")
        root_id = _string(root.get("id"), "root ID")
        try:
            first_tree = await receiver.read_tree(address=address, cycle_id=cycle_id)
            snapshot_cursor = _string(first_tree.get("snapshot_cursor"), "snapshot cursor")
            nodes = first_tree.get("nodes")
            if not isinstance(nodes, list) or len(nodes) != 1:
                raise ProofError("The initial tree does not contain only its root.")
            print(WAITING_MARKER, flush=True)

            while True:
                try:
                    await receiver.read_tree(
                        address=address,
                        cycle_id=cycle_id,
                        snapshot_cursor=snapshot_cursor,
                    )
                except StarTunnelError as error:
                    if not _interruption_is_safe(error):
                        raise ProofError(
                            "The interrupted tree read did not fail safely."
                        ) from error
                    break
                await asyncio.sleep(0.05)
            print(INTERRUPTED_MARKER, flush=True)

            await _wait_for_api(sender)
            first_reply = await receiver.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "json", "value": {"proof": "activity-restart"}},
                correlation_id="activity-restart",
                idempotency_key=reply_key,
            )
            retry_reply = await receiver.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "json", "value": {"proof": "activity-restart"}},
                correlation_id="activity-restart",
                idempotency_key=reply_key,
            )
            first_message = _object(first_reply.get("message"), "first reply")
            retry_message = _object(retry_reply.get("message"), "retry reply")
            reply_id = _string(first_message.get("id"), "reply ID")
            if _string(retry_message.get("id"), "retry reply ID") != reply_id:
                raise ProofError("The idempotent reply retry returned a different message.")

            recovered = await sender.read_tree(address=address, cycle_id=cycle_id)
            recovered_nodes = recovered.get("nodes")
            if not isinstance(recovered_nodes, list) or [
                _object(node, "tree node").get("id") for node in recovered_nodes
            ] != [root_id, reply_id]:
                raise ProofError("The recovered tree does not contain the expected reply.")
            await sender.close_cycle(
                address=address,
                expected_cycle_id=cycle_id,
                idempotency_key=close_key,
            )
            closed = True
            retained = await receiver.read_tree(address=address, cycle_id=cycle_id)
            retained_nodes = retained.get("nodes")
            if not isinstance(retained_nodes, list) or len(retained_nodes) != 2:
                raise ProofError("The closed cycle did not retain its tree.")
            print(COMPLETE_MARKER, flush=True)
        finally:
            if not closed:
                with suppress(StarTunnelError):
                    await sender.close_cycle(
                        address=address,
                        expected_cycle_id=cycle_id,
                        idempotency_key=close_key,
                    )


def _revoke_fixture(values: dict[str, str]) -> None:
    credential_ids = [
        UUID(values[name])
        for name in (
            "STARTUNNEL_SENDER_ID",
            "STARTUNNEL_RECEIVER_ID",
        )
        if values.get(name)
    ]
    if not credential_ids or not os.getenv("DATABASE_URL"):
        return
    with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as connection:
        connection.execute(
            "UPDATE agents_agentcredential SET revoked_at = now() WHERE id = ANY(%s)",
            (credential_ids,),
        )


def main() -> int:
    os.umask(0o077)
    fixture = _fixture_path()
    values: dict[str, str] = {}
    credentials_revoked = False
    stage = "CONFIGURATION"
    try:
        base_url = _validate_base_url(os.environ.get("STARTUNNEL_BASE_URL", ""))
        stage = "PROVISIONING"
        values = _provision(fixture)
        stage = "EXCHANGE"
        asyncio.run(_run_public_proof(values, base_url))
        stage = "REVOCATION"
        _revoke_fixture(values)
        credentials_revoked = True
    except Exception:
        print(FAILED_MARKER, flush=True)
        print(f"{FAILURE_STAGE_PREFIX}{stage}", flush=True)
        return 1
    finally:
        fixture.unlink(missing_ok=True)
        if not credentials_revoked:
            with suppress(OSError, ValueError, psycopg.Error):
                _revoke_fixture(values)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
