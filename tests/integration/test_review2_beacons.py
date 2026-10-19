"""PostgreSQL proves shared Beacon counts, first writes, and receipt deletion."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

import tunnels.beacons as beacons
from accounts.admin_commands import BeaconWithdraw
from accounts.admin_services import execute_admin_operation
from accounts.models import AdminReceipt
from agents.models import AgentCredential
from core.errors import CycleClosed, InvalidRequest, TunnelUnavailable
from core.models import AgentReceipt
from tunnels.cycles import close_cycle, create_tunnel
from tunnels.messages import post_reply
from tunnels.models import Beacon, BoardState, Cycle, Tunnel
from tunnels.retention import delete_due_cycle_content

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


def test_first_publications_on_different_tunnels_share_one_board_counter(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    publishers = [credential_factory()[0] for _ in range(2)]
    tunnels = [
        create_tunnel(
            credential=credential,
            idempotency_key=f"first-board-create-{index}",
            root_content={"type": "text", "text": "Review"},
        )
        for index, credential in enumerate(publishers)
    ]
    assert not BoardState.objects.exists()
    ready = Barrier(2)
    advance = beacons._advance_board_position

    def simultaneous_first_write() -> int:
        ready.wait(timeout=15)
        return advance()

    monkeypatch.setattr(beacons, "_advance_board_position", simultaneous_first_write)

    def publish(index: int) -> int:
        close_old_connections()
        try:
            credential = AgentCredential.objects.get(pk=publishers[index].id)
            result, _ = beacons.publish_beacon(
                credential=credential,
                address=tunnels[index].address,
                idempotency_key=f"first-board-publish-{index}",
                text="Review this",
            )
            return int(result["board_position"])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        positions = list(executor.map(publish, range(2)))
    assert sorted(positions) == [1, 2]
    assert BoardState.objects.get(pk=1).position == 2
    assert Beacon.objects.count() == 2


def test_concurrent_distinct_responders_are_counted_once(credential_factory: Any) -> None:
    publisher, _ = credential_factory()
    responders = [credential_factory()[0] for _ in range(3)]
    created = create_tunnel(
        credential=publisher,
        idempotency_key="count-responders-create",
        root_content={"type": "text", "text": "Review"},
    )
    published, _ = beacons.publish_beacon(
        credential=publisher,
        address=created.address,
        idempotency_key="count-responders-publish",
        text="Review this",
    )
    ready = Barrier(len(responders))

    def reply(index: int) -> None:
        close_old_connections()
        try:
            credential = AgentCredential.objects.get(pk=responders[index].id)
            ready.wait(timeout=15)
            for repetition in range(2):
                post_reply(
                    credential=credential,
                    address=created.address,
                    parent_id=created.root.id,
                    idempotency_key=f"count-reply-{index}-{repetition}",
                    content={"type": "text", "text": "I can review"},
                )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(reply, range(3)))
    assert (
        beacons.beacon_data(beacons.beacon_queryset().get(pk=published["id"]))["response_count"]
        == 3
    )
    assert BoardState.objects.get(pk=1).position == 4


def test_retention_clears_agent_and_admin_beacon_results(
    credential_factory: Any, user_factory: Any
) -> None:
    publisher, _ = credential_factory()
    actor = user_factory()
    created = create_tunnel(
        credential=publisher,
        idempotency_key="purge-beacon-create",
        root_content={"type": "text", "text": "Review"},
    )
    published, _ = beacons.publish_beacon(
        credential=publisher,
        address=created.address,
        idempotency_key="purge-beacon-publish",
        text="Private work summary",
    )
    request = {"beacon_id": published["id"]}
    execute_admin_operation(
        actor=actor,
        command=BeaconWithdraw(beacon_id=UUID(published["id"])),
        key="purge-beacon-withdraw",
        request_data=request,
    )
    close_cycle(
        credential=publisher,
        address=created.address,
        idempotency_key="purge-beacon-close",
        expected_cycle_id=created.cycle.id,
    )
    created.cycle.refresh_from_db()
    assert created.cycle.delete_after is not None
    assert delete_due_cycle_content(
        cycle_id=created.cycle.id,
        now=created.cycle.delete_after + timedelta(seconds=1),
    )
    assert not Beacon.objects.exists()
    assert AgentReceipt.objects.get(operation="publish_beacon").response == {}
    assert AdminReceipt.objects.get(operation="beacons.withdraw").response == {}
    with pytest.raises(TunnelUnavailable):
        beacons.publish_beacon(
            credential=publisher,
            address=created.address,
            idempotency_key="purge-beacon-publish",
            text="Private work summary",
        )
    with pytest.raises(InvalidRequest):
        execute_admin_operation(
            actor=actor,
            command=BeaconWithdraw(beacon_id=UUID(published["id"])),
            key="purge-beacon-withdraw",
            request_data=request,
        )


def test_publication_rechecks_expiry_after_waiting_for_tunnel_lock(
    credential_factory: Any,
) -> None:
    publisher, _ = credential_factory()
    created = create_tunnel(
        credential=publisher,
        idempotency_key="blocked-publication-create",
        root_content={"type": "text", "text": "Review"},
    )
    started = Event()
    backend: list[int] = []

    def publish() -> None:
        close_old_connections()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET statement_timeout = '15s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend.append(cursor.fetchone()[0])
            started.set()
            beacons.publish_beacon(
                credential=AgentCredential.objects.get(pk=publisher.pk),
                address=created.address,
                idempotency_key="blocked-publication-publish",
                text="Too late after the lock",
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with transaction.atomic():
            Tunnel.objects.select_for_update().get(pk=created.tunnel.pk)
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                owner = cursor.fetchone()[0]
            result = executor.submit(publish)
            assert started.wait(timeout=5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_stat_clear_snapshot()")
                    cursor.execute(
                        "SELECT wait_event_type = 'Lock' AND %s = ANY(pg_blocking_pids(pid)) "
                        "FROM pg_stat_activity WHERE pid = %s",
                        [owner, backend[0]],
                    )
                    row = cursor.fetchone()
                if row and row[0]:
                    break
            else:
                pytest.fail("Publication did not wait for the tunnel row lock.")
            Cycle.objects.filter(pk=created.cycle.pk).update(
                expires_at=timezone.now() - timedelta(seconds=1),
            )
        with pytest.raises(CycleClosed):
            result.result(timeout=15)
    assert not Beacon.objects.exists()
    assert not BoardState.objects.exists()
    assert not AgentReceipt.objects.filter(operation="publish_beacon").exists()
