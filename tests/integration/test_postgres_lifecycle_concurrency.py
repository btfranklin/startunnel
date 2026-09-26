"""Real PostgreSQL races prove stable-address lifecycle serialization."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest
from django.db import close_old_connections, connection

from accounts.models import User
from agents.models import AgentCredential
from tunnels.errors import CycleClosed, LifecycleConflict, TunnelUnavailable
from tunnels.models import Cycle, Message, Tunnel, TunnelAddress, TunnelEvent
from tunnels.services import (
    close_cycle,
    create_tunnel,
    post_reply,
    retire_tunnel_as_operator,
    rollover_cycle,
    rotate_address_as_operator,
    start_cycle,
)

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL lifecycle tests.")


def _thread_credential(credential_id: UUID) -> AgentCredential:
    return AgentCredential.objects.select_related("created_by").get(pk=credential_id)


def test_two_starts_create_only_one_next_cycle(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-start-create",
        root_content={"type": "text", "text": "First root."},
    )
    close_cycle(
        credential=creator,
        idempotency_key="pg-start-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    barrier = threading.Barrier(2)

    def attempt(index: int) -> str:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            barrier.wait(timeout=15)
            try:
                start_cycle(
                    credential=credential,
                    idempotency_key=f"pg-concurrent-start-{index}",
                    address=created.address,
                    expected_address_generation=1,
                    root_content={"type": "text", "text": f"Candidate root {index}."},
                )
            except LifecycleConflict:
                return "conflict"
            return "started"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))

    tunnel = Tunnel.objects.get(pk=created.tunnel.id)
    cycles = list(Cycle.objects.filter(tunnel=tunnel).order_by("number"))
    assert sorted(outcomes) == ["conflict", "started"]
    assert [cycle.number for cycle in cycles] == [1, 2]
    assert cycles[1].state == Cycle.State.ACTIVE
    assert tunnel.state == Tunnel.State.ACTIVE
    assert tunnel.next_cycle_number == 3


def test_two_rollovers_close_once_and_start_once(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-rollover-create",
        root_content={"type": "text", "text": "First root."},
    )
    barrier = threading.Barrier(2)

    def attempt(index: int) -> str:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            barrier.wait(timeout=15)
            try:
                rollover_cycle(
                    credential=credential,
                    idempotency_key=f"pg-concurrent-rollover-{index}",
                    address=created.address,
                    expected_cycle_id=created.cycle.id,
                    expected_address_generation=1,
                    root_content={"type": "text", "text": f"Next root {index}."},
                )
            except CycleClosed, LifecycleConflict:
                return "conflict"
            return "rolled"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, range(2)))

    tunnel = Tunnel.objects.get(pk=created.tunnel.id)
    cycles = list(Cycle.objects.filter(tunnel=tunnel).order_by("number"))
    events = list(TunnelEvent.objects.filter(tunnel=tunnel).order_by("position"))
    assert sorted(outcomes) == ["conflict", "rolled"]
    assert [cycle.state for cycle in cycles] == [
        Cycle.State.CLOSED,
        Cycle.State.ACTIVE,
    ]
    assert [event.position for event in events] == list(range(1, 7))
    assert [event.event_type for event in events] == [
        TunnelEvent.Type.CYCLE_STARTED,
        TunnelEvent.Type.MESSAGE_POSTED,
        TunnelEvent.Type.CYCLE_CLOSED,
        TunnelEvent.Type.TUNNEL_DORMANT,
        TunnelEvent.Type.CYCLE_STARTED,
        TunnelEvent.Type.MESSAGE_POSTED,
    ]


def test_rollover_racing_close_has_one_coherent_final_state(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-rollover-close-create",
        root_content={"type": "text", "text": "First root."},
    )
    barrier = threading.Barrier(2)

    def do_close() -> str:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            barrier.wait(timeout=15)
            try:
                close_cycle(
                    credential=credential,
                    idempotency_key="pg-rollover-close-close",
                    address=created.address,
                    expected_cycle_id=created.cycle.id,
                )
            except CycleClosed, LifecycleConflict:
                return "conflict"
            return "closed"
        finally:
            close_old_connections()

    def do_rollover() -> str:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            barrier.wait(timeout=15)
            try:
                rollover_cycle(
                    credential=credential,
                    idempotency_key="pg-rollover-close-rollover",
                    address=created.address,
                    expected_cycle_id=created.cycle.id,
                    expected_address_generation=1,
                    root_content={"type": "text", "text": "Second root."},
                )
            except CycleClosed, LifecycleConflict:
                return "conflict"
            return "rolled"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        close_future = executor.submit(do_close)
        rollover_future = executor.submit(do_rollover)
        outcomes = [close_future.result(timeout=30), rollover_future.result(timeout=30)]

    tunnel = Tunnel.objects.get(pk=created.tunnel.id)
    cycles = list(Cycle.objects.filter(tunnel=tunnel).order_by("number"))
    assert outcomes.count("conflict") == 1
    if "closed" in outcomes:
        assert len(cycles) == 1
        assert tunnel.state == Tunnel.State.DORMANT
    else:
        assert outcomes == ["conflict", "rolled"] or outcomes == ["rolled", "conflict"]
        assert len(cycles) == 2
        assert tunnel.state == Tunnel.State.ACTIVE
    assert Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).count() <= 1


def test_rotation_racing_post_commits_before_rotation_or_fails_cleanly(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory()
    worker, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-rotation-post-create",
        root_content={"type": "text", "text": "First root."},
    )
    barrier = threading.Barrier(2)

    def rotate() -> str:
        close_old_connections()
        try:
            actor = User.objects.get(pk=creator.created_by_id)
            barrier.wait(timeout=15)
            rotate_address_as_operator(
                actor=actor,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
            )
            return "rotated"
        finally:
            close_old_connections()

    def post() -> str:
        close_old_connections()
        try:
            credential = _thread_credential(worker.id)
            barrier.wait(timeout=15)
            try:
                post_reply(
                    credential=credential,
                    idempotency_key="pg-rotation-post-reply",
                    address=created.address,
                    parent_id=created.root.id,
                    content={"type": "text", "text": "Racing reply."},
                )
            except TunnelUnavailable:
                return "unavailable"
            return "posted"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [executor.submit(rotate), executor.submit(post)]
        results = [future.result(timeout=30) for future in outcomes]

    addresses = list(TunnelAddress.objects.filter(tunnel=created.tunnel).order_by("generation"))
    cycle = Cycle.objects.get(pk=created.cycle.id)
    assert results[0] == "rotated"
    assert results[1] in {"posted", "unavailable"}
    assert [(row.generation, row.state) for row in addresses] == [
        (1, TunnelAddress.State.RETIRED),
        (2, TunnelAddress.State.CURRENT),
    ]
    assert cycle.message_count == (2 if results[1] == "posted" else 1)
    assert Message.objects.filter(cycle=cycle).count() == cycle.message_count


def test_retirement_racing_start_always_ends_retired(credential_factory: Any) -> None:
    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-retire-start-create",
        label="Retire race board",
        root_content={"type": "text", "text": "First root."},
    )
    close_cycle(
        credential=creator,
        idempotency_key="pg-retire-start-close",
        address=created.address,
        expected_cycle_id=created.cycle.id,
    )
    barrier = threading.Barrier(2)

    def retire() -> str:
        close_old_connections()
        try:
            actor = User.objects.get(pk=creator.created_by_id)
            barrier.wait(timeout=15)
            retire_tunnel_as_operator(
                actor=actor,
                tunnel_id=created.tunnel.id,
                expected_address_generation=1,
                confirmation="Retire race board",
            )
            return "retired"
        finally:
            close_old_connections()

    def start() -> str:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            barrier.wait(timeout=15)
            try:
                start_cycle(
                    credential=credential,
                    idempotency_key="pg-retire-start-start",
                    address=created.address,
                    expected_address_generation=1,
                    root_content={"type": "text", "text": "Second root."},
                )
            except LifecycleConflict, TunnelUnavailable:
                return "rejected"
            return "started"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(retire), executor.submit(start)]
        outcomes = [future.result(timeout=30) for future in results]

    tunnel = Tunnel.objects.get(pk=created.tunnel.id)
    cycles = list(Cycle.objects.filter(tunnel=tunnel).order_by("number"))
    assert outcomes[0] == "retired"
    assert outcomes[1] in {"rejected", "started"}
    assert tunnel.state == Tunnel.State.RETIRED
    assert not TunnelAddress.objects.filter(
        tunnel=tunnel, state=TunnelAddress.State.CURRENT
    ).exists()
    assert not Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).exists()
    assert len(cycles) == (2 if outcomes[1] == "started" else 1)


@pytest.mark.parametrize("operation", ["close", "rollover"])
def test_concurrent_identical_lifecycle_retries_replay_success(
    credential_factory: Any, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    from tunnels.idempotency import find_replay

    creator, _ = credential_factory()
    created = create_tunnel(
        credential=creator,
        idempotency_key="pg-exact-retry-create",
        root_content={"type": "text", "text": "First root."},
    )
    barrier = threading.Barrier(2)

    def synchronized_initial_replay(**kwargs: Any) -> Any:
        found = find_replay(**kwargs)
        barrier.wait(timeout=15)
        return found

    monkeypatch.setattr("tunnels.services.find_idempotency_replay", synchronized_initial_replay)

    def attempt(_: int) -> UUID | None:
        close_old_connections()
        try:
            credential = _thread_credential(creator.id)
            if operation == "close":
                close_cycle(
                    credential=credential,
                    address=created.address,
                    expected_cycle_id=created.cycle.id,
                    idempotency_key="pg-identical-lifecycle-retry",
                )
                return None
            return rollover_cycle(
                credential=credential,
                address=created.address,
                expected_cycle_id=created.cycle.id,
                idempotency_key="pg-identical-lifecycle-retry",
                expected_address_generation=1,
                root_content={"type": "text", "text": "Next root."},
            ).cycle.id
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert results[0] == results[1]
    assert Cycle.objects.filter(tunnel=created.tunnel).count() == (
        2 if operation == "rollover" else 1
    )
    assert (
        TunnelEvent.objects.filter(
            tunnel=created.tunnel, event_type=TunnelEvent.Type.CYCLE_CLOSED
        ).count()
        == 1
    )
