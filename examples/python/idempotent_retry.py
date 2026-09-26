"""Replay stable-tree writes and reject one changed request."""

from __future__ import annotations

import asyncio

from startunnel_client import (
    JsonObject,
    StarTunnelClient,
    StarTunnelError,
    load_env_file,
    new_idempotency_key,
    require_environment,
)


async def run() -> None:
    load_env_file()
    env = require_environment("STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY")
    client = StarTunnelClient(env["STARTUNNEL_BASE_URL"], env["STARTUNNEL_SENDER_KEY"])
    address: str | None = None
    cycle_id: str | None = None
    closed = False
    try:
        create_key = new_idempotency_key()

        async def create_once() -> JsonObject:
            return await client.create_tunnel(
                label="Idempotent retry example",
                cycle_label="Retry proof",
                expires_in_seconds=3600,
                root_content={"type": "text", "text": "This root is created once."},
                root_correlation_id="idempotent-example",
                idempotency_key=create_key,
            )

        first = await create_once()
        replayed = await create_once()
        first_tunnel = first.get("tunnel")
        replayed_tunnel = replayed.get("tunnel")
        first_cycle = first.get("cycle")
        replayed_cycle = replayed.get("cycle")
        if not all(
            isinstance(value, dict)
            for value in (first_tunnel, replayed_tunnel, first_cycle, replayed_cycle)
        ):
            raise RuntimeError("A create response was not valid.")
        assert isinstance(first_tunnel, dict)
        assert isinstance(replayed_tunnel, dict)
        assert isinstance(first_cycle, dict)
        assert isinstance(replayed_cycle, dict)
        if first_tunnel.get("id") != replayed_tunnel.get("id") or first_cycle.get(
            "id"
        ) != replayed_cycle.get("id"):
            raise RuntimeError("The create retry returned a different tunnel or cycle.")
        if replayed.get("idempotent_replay") is not True:
            raise RuntimeError("The repeated create response was not marked as a replay.")
        address = str(first_tunnel["address"])
        cycle_id = str(first_cycle["id"])
        root = first_cycle.get("root")
        if not isinstance(root, dict):
            raise RuntimeError("The create response did not contain the root message.")
        root_id = str(root["id"])
        print("The identical create retry returned the first tunnel, cycle, and root.")

        reply_key = new_idempotency_key()

        async def post_once() -> JsonObject:
            return await client.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "text", "text": "This logical reply is stored once."},
                correlation_id="idempotent-example",
                idempotency_key=reply_key,
            )

        first_reply = await post_once()
        replayed_reply = await post_once()
        first_summary = first_reply.get("message")
        replayed_summary = replayed_reply.get("message")
        if not isinstance(first_summary, dict) or not isinstance(replayed_summary, dict):
            raise RuntimeError("A reply response was not valid.")
        if first_summary.get("id") != replayed_summary.get("id"):
            raise RuntimeError("The reply retry returned a different message.")
        if replayed_reply.get("idempotent_replay") is not True:
            raise RuntimeError("The repeated reply response was not marked as a replay.")
        print("The identical reply retry returned the first immutable message.")

        try:
            await client.post_reply(
                address=address,
                parent_id=root_id,
                content={"type": "text", "text": "This body is different."},
                correlation_id="idempotent-example",
                idempotency_key=reply_key,
            )
        except StarTunnelError as error:
            if error.status_code != 409 or error.code != "idempotency_conflict":
                raise
            print("A changed request with the same key returned 409 idempotency_conflict.")
        else:
            raise RuntimeError("The changed request reused a retry key without a conflict.")

        await client.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
    finally:
        if address is not None and cycle_id is not None and not closed:
            await client.close_cycle(
                address=address,
                expected_cycle_id=cycle_id,
                idempotency_key=new_idempotency_key(),
            )
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(run())
