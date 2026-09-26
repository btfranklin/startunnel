"""Close one cycle, reject a late reply, and read the retained tree."""

from __future__ import annotations

import asyncio

from startunnel_client import (
    StarTunnelClient,
    StarTunnelError,
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
            label="Cycle closure example",
            root_content={"type": "text", "text": "Record this decision."},
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

        posted = await receiver.post_reply(
            address=address,
            parent_id=root_id,
            content={"type": "text", "text": "Decision recorded."},
            idempotency_key=new_idempotency_key(),
        )
        if not isinstance(posted.get("message"), dict):
            raise RuntimeError("The reply response was not valid.")
        print("The receiver posted one reply in the active cycle.")

        await sender.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        print("The creator closed the cycle.")

        try:
            await receiver.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "text", "text": "This reply is too late."},
                idempotency_key=new_idempotency_key(),
            )
        except StarTunnelError as error:
            if error.status_code != 409:
                raise
            print("A reply after closure returned HTTP 409 as expected.")
        else:
            raise RuntimeError("The closed cycle accepted a new reply.")

        retained = await receiver.read_tree(address=address, cycle_id=cycle_id)
        nodes = retained.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != 2:
            raise RuntimeError("The retained tree did not contain both messages.")
        print("The same stable address still reads the closed cycle.")
        print("Cycle closure is complete.")
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
