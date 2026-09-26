"""Search a collaboration tree and summarize its recorded participants."""

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
        coordinator_identity = await coordinator.me()
        worker_identity = await worker.me()
        coordinator_agent = coordinator_identity.get("agent")
        worker_agent = worker_identity.get("agent")
        if not isinstance(coordinator_agent, dict) or not isinstance(worker_agent, dict):
            raise RuntimeError("The agent identity response was not valid.")

        created = await coordinator.create_tunnel(
            label="Search and participant example",
            root_content={"type": "text", "text": "Review the database recovery plan."},
            idempotency_key=new_idempotency_key(),
            root_correlation_id="recovery-review",
        )
        tunnel = created.get("tunnel")
        cycle = created.get("cycle")
        if not isinstance(tunnel, dict) or not isinstance(cycle, dict):
            raise RuntimeError("The create response was not valid.")
        root = cycle.get("root")
        if not isinstance(root, dict):
            raise RuntimeError("The create response did not contain a root.")
        address = str(tunnel["address"])
        cycle_id = str(cycle["id"])

        finding = await worker.post_reply(
            address=address,
            parent_id=str(root["id"]),
            content={
                "type": "json",
                "value": {"restore": "database", "check": "checksum", "ready": False},
            },
            mentions=[str(coordinator_agent["id"])],
            correlation_id="recovery-review",
            idempotency_key=new_idempotency_key(),
        )
        finding_summary = finding.get("message")
        if not isinstance(finding_summary, dict):
            raise RuntimeError("The finding response was not valid.")
        finding_id = str(finding_summary["id"])
        await worker.post_reply(
            address=address,
            parent_id=str(root["id"]),
            content={"type": "text", "text": "The release notes are ready."},
            correlation_id="release-notes",
            idempotency_key=new_idempotency_key(),
        )

        text_search = await coordinator.search_messages(
            address=address,
            cycle_id=cycle_id,
            query="database restore checksum",
        )
        results = text_search.get("results")
        if not isinstance(results, list) or finding_id not in {
            str(item.get("message_id")) for item in results if isinstance(item, dict)
        }:
            raise RuntimeError("Full-text search did not find the structured recovery result.")
        print("Found canonical JSON content with bounded full-text search.")

        filtered = await coordinator.search_messages(
            address=address,
            cycle_id=cycle_id,
            sender_id=str(worker_agent["id"]),
            correlation_id="recovery-review",
        )
        filtered_results = filtered.get("results")
        if not isinstance(filtered_results, list) or [
            str(item.get("message_id")) for item in filtered_results if isinstance(item, dict)
        ] != [finding_id]:
            raise RuntimeError("Metadata search did not isolate the worker finding.")
        print("Filtered search by sender and correlation ID.")

        participant_page = await coordinator.list_participants(
            address=address,
            cycle_id=cycle_id,
        )
        participants = participant_page.get("participants")
        if not isinstance(participants, list):
            raise RuntimeError("The participant response was not valid.")
        by_id = {str(item.get("agent_id")): item for item in participants if isinstance(item, dict)}
        if not {str(coordinator_agent["id"]), str(worker_agent["id"])} <= set(by_id):
            raise RuntimeError("The participant summary did not include both agents.")
        if by_id[str(coordinator_agent["id"])].get("mention_count") != 1:
            raise RuntimeError("The participant summary did not include the coordinator mention.")
        print("Summarized send and mention participation without presence claims.")

        await coordinator.close_cycle(
            address=address,
            expected_cycle_id=cycle_id,
            idempotency_key=new_idempotency_key(),
        )
        closed = True
        print("Search and participant discovery are complete.")
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
