"""Post several concurrent replies below one root and read the result."""

from __future__ import annotations

import asyncio
import os

from startunnel_client import (
    JsonObject,
    StarTunnelClient,
    load_env_file,
    new_idempotency_key,
    require_environment,
)


async def run() -> None:
    load_env_file()
    env = require_environment("STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY")
    raw_keys = os.environ.get("STARTUNNEL_RECEIVER_KEYS", "")
    receiver_keys = list(dict.fromkeys(key.strip() for key in raw_keys.split(",") if key.strip()))
    if len(receiver_keys) < 2:
        raise ValueError(
            "STARTUNNEL_RECEIVER_KEYS must contain at least two different keys, "
            "separated by commas."
        )

    sender = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_SENDER_KEY"])
    receivers = [StarTunnelClient(env["STARTUNNEL_BASE_URL"], key) for key in receiver_keys]
    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        created = await sender.create_tunnel(
            label="Concurrent reply example",
            root_content={"type": "json", "value": {"question": "Report worker status."}},
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

        start = asyncio.Event()

        async def post(client: StarTunnelClient, worker: int) -> JsonObject:
            await start.wait()
            return await client.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "json", "value": {"worker": worker, "status": "ready"}},
                correlation_id="concurrent-replies",
                idempotency_key=new_idempotency_key(),
            )

        tasks = [
            asyncio.create_task(post(client, worker))
            for worker, client in enumerate(receivers, start=1)
        ]
        start.set()
        results = await asyncio.gather(*tasks)
        summaries = [result.get("message") for result in results]
        if not all(isinstance(summary, dict) for summary in summaries):
            raise RuntimeError("A concurrent reply response was not valid.")
        sequences = [summary["sequence"] for summary in summaries if isinstance(summary, dict)]
        if len(set(sequences)) != len(receivers):
            raise RuntimeError("Concurrent replies did not receive unique sequence values.")

        direct = await sender.read_replies(
            address=address,
            cycle_id=cycle_id,
            message_id=root_id,
            limit=len(receivers),
        )
        messages = direct.get("messages")
        if not isinstance(messages, list) or len(messages) != len(receivers):
            raise RuntimeError("The root did not contain every concurrent reply.")
        print(f"Concurrent agents started: {len(receivers)}")
        print(f"Immutable direct replies stored: {len(messages)}")
        print("Every concurrent reply has a unique sequence value.")

        await sender.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
    finally:
        if address is not None and cycle_id is not None and not closed:
            await sender.close_cycle(
                address=address,
                expected_cycle_id=cycle_id,
                idempotency_key=new_idempotency_key(),
            )
        await sender.aclose()
        for receiver in receivers:
            await receiver.aclose()


if __name__ == "__main__":
    asyncio.run(run())
