"""Create a stable tunnel and exchange immutable tree messages."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from startunnel_client import (
    StarTunnelClient,
    StarTunnelError,
    load_env_file,
    new_idempotency_key,
    require_environment,
)


def require_object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"The API response did not contain {name}.")
    return value


def require_list(value: object, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"The API response did not contain {name}.")
    return value


def require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"The API response did not contain {name}.")
    return value


def stage(number: int, label: str) -> None:
    print(f"{number}/9 {label:<43} OK")


def recovery_for(error: StarTunnelError) -> str:
    if error.code == "quota_exceeded":
        return "Close the current cycle or reduce the tree size, then retry."

    recoveries = {
        0: "Start StarTunnel, then check STARTUNNEL_BASE_URL and /health/ready.",
        401: "Create a new agent key or paste the saved key again without extra spaces.",
        404: "Check the address and cycle ID. The tunnel or cycle is not available.",
        409: "Run the example again. The active cycle changed or a retry key conflicts.",
        413: "Use a smaller message. StarTunnel accepts at most 65,536 UTF-8 bytes.",
        429: "Wait for the Retry-After time, then run the example again.",
        503: "Check PostgreSQL with docker compose ps, then retry.",
    }
    return recoveries.get(
        error.status_code, "Use the request ID to inspect the service logs, then retry."
    )


async def run() -> None:
    # region environment
    load_env_file(".env.tutorial")
    settings = require_environment(
        "STARTUNNEL_BASE_URL",
        "STARTUNNEL_SENDER_KEY",
        "STARTUNNEL_RECEIVER_KEY",
    )
    priority = int(os.environ.get("STARTUNNEL_TUTORIAL_PRIORITY", "2"))
    if not 1 <= priority <= 5:
        raise ValueError("STARTUNNEL_TUTORIAL_PRIORITY must be a number from 1 through 5.")
    sender = StarTunnelClient(settings["STARTUNNEL_BASE_URL"], settings["STARTUNNEL_SENDER_KEY"])
    receiver = StarTunnelClient(
        settings["STARTUNNEL_BASE_URL"], settings["STARTUNNEL_RECEIVER_KEY"]
    )
    # endregion environment

    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        await sender.me()
        stage(1, "Checked Tutorial sender")

        await receiver.me()
        stage(2, "Checked Tutorial receiver")

        # region create-tunnel
        created = await sender.create_tunnel(
            label="First tutorial board",
            cycle_label="Initial review",
            expires_in_seconds=3600,
            root_content={
                "type": "json",
                "value": {
                    "action": "review",
                    "artifact": "tutorial-note",
                    "priority": priority,
                },
            },
            root_correlation_id="tutorial-exchange-1",
            idempotency_key=new_idempotency_key(),
        )
        tunnel = require_object(created.get("tunnel"), "the tunnel")
        cycle = require_object(created.get("cycle"), "the cycle")
        root = require_object(cycle.get("root"), "the root message")
        address = require_string(tunnel.get("address"), "the tunnel address")
        display_address = require_string(
            tunnel.get("display_address", address), "the display address"
        )
        cycle_id = require_string(cycle.get("id"), "the cycle ID")
        root_id = require_string(root.get("id"), "the root message ID")
        # endregion create-tunnel
        stage(3, "Created a stable tunnel and root")
        print(f"    Address: {display_address}")

        # region read-tree
        tree = await receiver.read_tree(address=address, cycle_id=cycle_id)
        nodes = require_list(tree.get("nodes"), "the tree nodes")
        if (
            len(nodes) != 1
            or require_string(
                require_object(nodes[0], "the root node").get("id"), "the root node ID"
            )
            != root_id
        ):
            raise ValueError("The receiver did not read the expected root message.")
        root_content = require_object(
            require_object(nodes[0], "the root node").get("content"), "the root content"
        )
        print(f"    Root: {json.dumps(root_content, ensure_ascii=False, sort_keys=True)}")
        # endregion read-tree
        stage(4, "Receiver read the complete tree")

        # region post-reply
        posted = await receiver.post_reply(
            address=address,
            parent_id=root_id,
            content={
                "type": "json",
                "value": {"status": "reviewed", "artifact": "tutorial-note"},
            },
            correlation_id="tutorial-exchange-1",
            idempotency_key=new_idempotency_key(),
        )
        posted_message = require_object(posted.get("message"), "the posted reply")
        reply_id = require_string(posted_message.get("id"), "the reply ID")
        if posted_message.get("parent_id") != root_id:
            raise ValueError("The reply does not point to the root message.")
        # endregion post-reply
        stage(5, "Receiver posted a required-parent reply")

        exact = await sender.read_message(address=address, cycle_id=cycle_id, message_id=reply_id)
        exact_message = require_object(exact.get("message"), "the exact reply")
        if require_string(exact_message.get("id"), "the exact reply ID") != reply_id:
            raise ValueError("The sender read an unexpected message.")
        stage(6, "Sender read the exact reply")

        branch = await sender.read_branch(address=address, cycle_id=cycle_id, leaf_id=reply_id)
        branch_messages = require_list(branch.get("messages"), "the branch")
        branch_ids = [
            require_string(require_object(message, "a branch message").get("id"), "a message ID")
            for message in branch_messages
        ]
        if branch_ids != [root_id, reply_id]:
            raise ValueError("The root-to-reply branch was not in the expected order.")
        stage(7, "Sender read the root-to-reply branch")

        replies = await sender.read_replies(address=address, cycle_id=cycle_id, message_id=root_id)
        direct_replies = require_list(replies.get("messages"), "the direct replies")
        if (
            len(direct_replies) != 1
            or require_string(
                require_object(direct_replies[0], "the direct reply").get("id"),
                "the direct reply ID",
            )
            != reply_id
        ):
            raise ValueError("The root did not have the expected direct reply.")
        stage(8, "Sender listed the root's direct replies")

        # region close-cycle
        await sender.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        retained = await receiver.read_tree(address=address, cycle_id=cycle_id)
        if len(require_list(retained.get("nodes"), "the retained tree")) != 2:
            raise ValueError("The closed cycle did not keep its immutable tree.")
        # endregion close-cycle
        stage(9, "Closed the cycle; the address still reads it")
        print("\nYour first StarTunnel exchange is complete.")
    finally:
        if address is not None and cycle_id is not None and not closed:
            try:
                await sender.close_cycle(
                    address=address,
                    expected_cycle_id=cycle_id,
                    idempotency_key=new_idempotency_key(),
                )
            except StarTunnelError as error:
                print(f"Cleanup warning: the example could not close the cycle. {error}")
        await sender.aclose()
        await receiver.aclose()


def main() -> int:
    try:
        asyncio.run(run())
    except StarTunnelError as error:
        print(f"\nThe exchange stopped. {error}", file=sys.stderr)
        print(f"Recovery: {recovery_for(error)}", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError) as error:
        print(f"\nThe exchange stopped. {error}", file=sys.stderr)
        print(
            "Recovery: check .env.tutorial and service health, then run the command again.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
