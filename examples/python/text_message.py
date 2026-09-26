"""Create one text root and add one required-parent text reply."""

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
    sender = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_SENDER_KEY"])
    receiver = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_RECEIVER_KEY"])
    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        created = await sender.create_tunnel(
            label="Small text example",
            root_content={"type": "text", "text": "Ready for review."},
            idempotency_key=new_idempotency_key(),
        )
        tunnel = created["tunnel"]
        cycle = created["cycle"]
        if not isinstance(tunnel, dict) or not isinstance(cycle, dict):
            raise RuntimeError("The create response was not valid.")
        root = cycle.get("root")
        if not isinstance(root, dict):
            raise RuntimeError("The create response did not contain the root message.")
        address = str(tunnel["address"])
        cycle_id = str(cycle["id"])
        root_id = str(root["id"])

        read = await receiver.read_message(address=address, cycle_id=cycle_id, message_id=root_id)
        message = read.get("message")
        if not isinstance(message, dict):
            raise RuntimeError("The root message was not valid.")
        content = message.get("content")
        if not isinstance(content, dict):
            raise RuntimeError("The root content was not valid.")
        print(f"Root text: {content.get('text')}")

        posted = await receiver.post_reply(
            address=address,
            parent_id=root_id,
            content={"type": "text", "text": "Review complete."},
            idempotency_key=new_idempotency_key(),
        )
        reply = posted.get("message")
        if not isinstance(reply, dict):
            raise RuntimeError("The reply response was not valid.")

        replies = await sender.read_replies(address=address, cycle_id=cycle_id, message_id=root_id)
        messages = replies.get("messages")
        if not isinstance(messages, list) or len(messages) != 1:
            raise RuntimeError("The root did not have one direct reply.")
        print("Read the root and posted one immutable reply.")

        await sender.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        print("Closed the cycle. The stable address still identifies the tunnel.")
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
