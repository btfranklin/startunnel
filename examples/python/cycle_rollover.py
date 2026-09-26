"""Keep one shared glyph address while a collaboration moves to its next cycle."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress

from startunnel_client import (
    StarTunnelClient,
    StarTunnelError,
    load_env_file,
    new_idempotency_key,
    require_environment,
)


async def run() -> None:
    load_env_file(names={"STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY"})
    settings = require_environment(
        "STARTUNNEL_BASE_URL", "STARTUNNEL_SENDER_KEY", "STARTUNNEL_RECEIVER_KEY"
    )
    address: str | None = None
    active_cycle_id: str | None = None
    async with (
        StarTunnelClient(
            settings["STARTUNNEL_BASE_URL"], settings["STARTUNNEL_SENDER_KEY"]
        ) as coordinator,
        StarTunnelClient(
            settings["STARTUNNEL_BASE_URL"], settings["STARTUNNEL_RECEIVER_KEY"]
        ) as reader,
    ):
        try:
            created = await coordinator.create_tunnel(
                label="Long-running release board",
                cycle_label="Release 42",
                root_content={"type": "text", "text": "Review release 42."},
                idempotency_key=new_idempotency_key(),
            )
            address = str(created["tunnel"]["address"])
            first_cycle_id = str(created["cycle"]["id"])
            status = await reader.get_tunnel_status(address=address)
            if status["address_generation"] != 1:
                raise RuntimeError("The new tunnel did not use address generation 1.")

            rolled = await coordinator.rollover_cycle(
                address=address,
                expected_cycle_id=first_cycle_id,
                expected_address_generation=1,
                cycle_label="Release 43",
                root_content={"type": "text", "text": "Review release 43."},
                idempotency_key=new_idempotency_key(),
            )
            active_cycle_id = str(rolled["cycle"]["id"])
            cycles = await reader.list_cycles(address=address)
            numbers = [item["number"] for item in cycles["items"]]  # type: ignore[index]
            if numbers != [2, 1]:
                raise RuntimeError("The cycle directory did not contain cycles 2 and 1.")
            current = await reader.read_tree(address=address, cycle_id=active_cycle_id)
            history = await reader.read_tree(address=address, cycle_id=first_cycle_id)
            if current["nodes"][0]["content"]["text"] != "Review release 43.":  # type: ignore[index]
                raise RuntimeError("The active tree did not contain the second root.")
            if history["nodes"][0]["content"]["text"] != "Review release 42.":  # type: ignore[index]
                raise RuntimeError("The closed tree did not preserve the first root.")
            print("One stable address now contains two bounded cycles.")
        finally:
            if address and active_cycle_id:
                with suppress(StarTunnelError):
                    await coordinator.close_cycle(
                        address=address,
                        expected_cycle_id=active_cycle_id,
                        idempotency_key=new_idempotency_key(),
                    )


def main() -> None:
    try:
        asyncio.run(run())
    except (StarTunnelError, ValueError, RuntimeError) as error:
        print(f"Cycle rollover failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
