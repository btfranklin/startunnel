"""Create a correlated JSON root and reply, then read their branch."""

from __future__ import annotations

import asyncio
import json

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
    sender = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_SENDER_KEY"])
    receiver = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_RECEIVER_KEY"])
    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        created = await sender.create_tunnel(
            label="Structured JSON example",
            root_content={
                "type": "json",
                "value": {"task": "review", "artifact": "report-42", "priority": 2},
            },
            root_correlation_id="report-42",
            idempotency_key=new_idempotency_key(),
        )
        tunnel = created.get("tunnel")
        cycle = created.get("cycle")
        if not isinstance(tunnel, dict) or not isinstance(cycle, dict):
            raise RuntimeError("The create response was not valid.")
        root = cycle.get("root")
        if not isinstance(root, dict):
            raise RuntimeError("The create response did not contain the root message.")
        address = str(tunnel["address"])
        cycle_id = str(cycle["id"])
        root_id = str(root["id"])

        task = await receiver.read_message(address=address, cycle_id=cycle_id, message_id=root_id)
        task_message = task.get("message")
        if not isinstance(task_message, dict):
            raise RuntimeError("The root message response was not valid.")
        print(f"Task: {json.dumps(task_message['content'], ensure_ascii=False, sort_keys=True)}")

        posted = await receiver.post_reply(
            address=address,
            parent_id=root_id,
            content={"type": "json", "value": {"status": "complete", "artifact": "report-42"}},
            correlation_id="report-42",
            idempotency_key=new_idempotency_key(),
        )
        posted_message = posted.get("message")
        if not isinstance(posted_message, dict):
            raise RuntimeError("The reply response was not valid.")
        reply_id = str(posted_message["id"])

        branch = await sender.read_branch(address=address, cycle_id=cycle_id, leaf_id=reply_id)
        messages = branch.get("messages")
        if not isinstance(messages, list) or len(messages) != 2:
            raise RuntimeError("The branch did not contain the root and reply.")
        reply = messages[-1]
        if not isinstance(reply, dict):
            raise RuntimeError("The reply in the branch was not valid.")
        print(f"Reply: {json.dumps(reply['content'], ensure_ascii=False, sort_keys=True)}")

        await sender.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        print("The correlated exchange is complete.")
    finally:
        if address is not None and cycle_id is not None and not closed:
            await sender.close_cycle(
                address=address,
                expected_cycle_id=cycle_id,
                idempotency_key=new_idempotency_key(),
            )
        await sender.aclose()
        await receiver.aclose()


if __name__ == "__main__":
    asyncio.run(run())
