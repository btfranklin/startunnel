"""Use subtree, leaves, activity, checkpoints, and bounded context."""

from __future__ import annotations

import asyncio

from startunnel_client import (
    StarTunnelClient,
    load_env_file,
    new_idempotency_key,
    require_environment,
)


async def run() -> None:
    load_env_file()
    env = require_environment(
        "STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY"
    )
    coordinator = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_SENDER_KEY"])
    worker = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_RECEIVER_KEY"])
    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        created = await coordinator.create_tunnel(
            label="Agent navigation example",
            root_content={"type": "text", "text": "Review the recovery plan."},
            idempotency_key=new_idempotency_key(),
        )
        tunnel = created.get("tunnel")
        cycle = created.get("cycle")
        initial_activity_cursor = created.get("activity_cursor")
        if not isinstance(tunnel, dict) or not isinstance(cycle, dict):
            raise RuntimeError("The create response was not valid.")
        root = cycle.get("root")
        if not isinstance(root, dict) or not isinstance(initial_activity_cursor, str):
            raise RuntimeError("The create response did not contain navigation values.")
        address = str(tunnel["address"])
        cycle_id = str(cycle["id"])
        root_id = str(root["id"])

        first = await worker.post_reply(
            address=address,
            parent_id=root_id,
            content={"type": "text", "text": "The restore role needs one grant."},
            idempotency_key=new_idempotency_key(),
        )
        first_summary = first.get("message")
        if not isinstance(first_summary, dict):
            raise RuntimeError("The first reply response was not valid.")
        first_id = str(first_summary["id"])
        second = await worker.post_reply(
            address=address,
            parent_id=first_id,
            content={"type": "json", "value": {"grant": "restore", "verified": False}},
            idempotency_key=new_idempotency_key(),
        )
        second_summary = second.get("message")
        if not isinstance(second_summary, dict):
            raise RuntimeError("The nested reply response was not valid.")
        second_id = str(second_summary["id"])

        subtree = await coordinator.read_subtree(
            address=address,
            cycle_id=cycle_id,
            message_id=root_id,
        )
        nodes = subtree.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 3:
            raise RuntimeError("The subtree did not contain the expected three nodes.")
        print("Read one three-node subtree in stable preorder.")

        leaves = await coordinator.read_leaves(address=address, cycle_id=cycle_id)
        leaf_rows = leaves.get("leaves")
        if not isinstance(leaf_rows, list) or [str(row.get("id")) for row in leaf_rows] != [
            second_id
        ]:
            raise RuntimeError("The leaf read did not identify the nested reply.")
        print("Found the current discussion leaf.")

        activity = await coordinator.read_activity(
            address=address,
            after_cursor=initial_activity_cursor,
        )
        events = activity.get("events")
        activity_cursor = activity.get("next_cursor")
        if not isinstance(events, list) or len(events) != 2 or not isinstance(activity_cursor, str):
            raise RuntimeError("The activity read did not contain both committed replies.")
        checkpoint = await coordinator.commit_checkpoint(
            address=address,
            cursor=activity_cursor,
        )
        if checkpoint.get("advanced") is not True:
            raise RuntimeError("The activity checkpoint did not advance.")
        print("Read two events and saved the processed checkpoint.")

        context = await coordinator.get_context(
            address=address,
            cycle_id=cycle_id,
            focus_message_id=second_id,
            referenced_message_ids=[first_id],
            max_items=10,
            max_bytes=32_768,
        )
        items = context.get("items")
        if not isinstance(items, list) or [item.get("reason") for item in items] != [
            "ancestor",
            "ancestor",
            "focus",
        ]:
            raise RuntimeError("The context packet did not preserve the complete branch.")
        print("Built one deterministic root-to-focus context packet.")

        await coordinator.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        print("Agent navigation and progress recovery are complete.")
    finally:
        if address is not None and cycle_id is not None and not closed:
            await coordinator.close_cycle(
                address=address,
                expected_cycle_id=cycle_id,
                idempotency_key=new_idempotency_key(),
            )
        await coordinator.aclose()
        await worker.aclose()


if __name__ == "__main__":
    asyncio.run(run())
