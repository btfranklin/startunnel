"""Real PostgreSQL tests protect durable activity history and checkpoints."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest
from django.db import DatabaseError, close_old_connections, connection, transaction

from agents.models import AgentCredential
from tunnels.models import ActivityCheckpoint, Tunnel, TunnelEvent
from tunnels.services import CreatedTunnel, close_cycle, create_tunnel, post_reply

pytestmark = [pytest.mark.postgres, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def require_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    if connection.vendor != "postgresql":
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integrity tests.")


def _create_board(credential: AgentCredential, *, key: str) -> CreatedTunnel:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Record durable activity."},
    )


def test_checkpoint_accepts_zero_and_committed_event_positions(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Activity creator")
    reader, _ = credential_factory(name="Activity reader")
    created = _create_board(creator, key="activity-valid-create")

    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=created.tunnel,
        credential=reader,
        last_event_position=0,
    )
    checkpoint.last_event_position = 2
    checkpoint.save(update_fields=["last_event_position", "updated_at"])

    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == 2
    assert TunnelEvent.objects.filter(tunnel=created.tunnel, position=2).exists()


def test_checkpoint_cannot_move_backward(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Backward creator")
    reader, _ = credential_factory(name="Backward reader")
    created = _create_board(creator, key="activity-backward-create")
    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=created.tunnel,
        credential=reader,
        last_event_position=2,
    )

    with pytest.raises(DatabaseError, match="cannot move backward"), transaction.atomic():
        ActivityCheckpoint.objects.filter(pk=checkpoint.pk).update(last_event_position=1)

    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == 2


def test_checkpoint_credential_cannot_be_reassigned(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Identity creator")
    first_reader, _ = credential_factory(name="First reader")
    second_reader, _ = credential_factory(name="Second reader")
    created = _create_board(creator, key="activity-credential-create")
    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=created.tunnel,
        credential=first_reader,
        last_event_position=0,
    )

    with pytest.raises(DatabaseError, match="identity is immutable"), transaction.atomic():
        ActivityCheckpoint.objects.filter(pk=checkpoint.pk).update(credential=second_reader)

    checkpoint.refresh_from_db()
    assert checkpoint.credential_id == first_reader.id


def test_checkpoint_tunnel_cannot_be_reassigned(credential_factory: Any) -> None:
    first_creator, _ = credential_factory(name="First tunnel creator")
    second_creator, _ = credential_factory(name="Second tunnel creator")
    reader, _ = credential_factory(name="Tunnel reader")
    first = _create_board(first_creator, key="activity-first-tunnel-create")
    second = _create_board(second_creator, key="activity-second-tunnel-create")
    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=first.tunnel,
        credential=reader,
        last_event_position=0,
    )

    with pytest.raises(DatabaseError, match="identity is immutable"), transaction.atomic():
        ActivityCheckpoint.objects.filter(pk=checkpoint.pk).update(tunnel=second.tunnel)

    checkpoint.refresh_from_db()
    assert checkpoint.tunnel_id == first.tunnel.id


def test_positive_checkpoint_must_name_an_existing_event(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Gap creator")
    reader, _ = credential_factory(name="Gap reader")
    created = _create_board(creator, key="activity-gap-create")
    TunnelEvent.objects.filter(tunnel=created.tunnel, position=1).delete()

    with (
        pytest.raises(DatabaseError, match="does not name a committed event"),
        transaction.atomic(),
    ):
        ActivityCheckpoint.objects.create(
            tunnel=created.tunnel,
            credential=reader,
            last_event_position=1,
        )

    assert not ActivityCheckpoint.objects.filter(
        tunnel=created.tunnel,
        credential=reader,
    ).exists()


def test_tunnel_event_cannot_be_updated_directly(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Immutable event creator")
    created = _create_board(creator, key="activity-event-create")
    event = TunnelEvent.objects.get(tunnel=created.tunnel, position=2)

    with pytest.raises(DatabaseError, match="tunnel events are immutable"), transaction.atomic():
        TunnelEvent.objects.filter(pk=event.pk).update(metadata={"changed": True})

    event.refresh_from_db()
    assert event.metadata == {}


def _advance_checkpoint(
    *,
    checkpoint_id: UUID,
    position: int,
    barrier: threading.Barrier,
) -> str:
    close_old_connections()
    try:
        barrier.wait(timeout=15)
        try:
            with transaction.atomic():
                ActivityCheckpoint.objects.filter(pk=checkpoint_id).update(
                    last_event_position=position
                )
        except DatabaseError:
            return "rejected"
        return f"updated:{position}"
    finally:
        close_old_connections()


def test_concurrent_checkpoint_writes_cannot_commit_a_regression(
    credential_factory: Any,
) -> None:
    creator, _ = credential_factory(name="Concurrent creator")
    reader, _ = credential_factory(name="Concurrent reader")
    created = _create_board(creator, key="activity-concurrent-create")
    checkpoint = ActivityCheckpoint.objects.create(
        tunnel=created.tunnel,
        credential=reader,
        last_event_position=0,
    )
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _advance_checkpoint,
                checkpoint_id=checkpoint.id,
                position=position,
                barrier=barrier,
            )
            for position in (1, 2)
        ]
        outcomes = [future.result(timeout=30) for future in futures]

    checkpoint.refresh_from_db()
    assert checkpoint.last_event_position == 2
    assert "updated:2" in outcomes
    assert outcomes.count("rejected") in {0, 1}
    assert set(outcomes) <= {"updated:1", "updated:2", "rejected"}


def test_checkpoint_cannot_exceed_committed_activity(credential_factory: Any) -> None:
    creator, _ = credential_factory(name="Future creator")
    reader, _ = credential_factory(name="Future reader")
    created = _create_board(creator, key="activity-future-create")
    created.tunnel.refresh_from_db()

    with (
        pytest.raises(DatabaseError, match="exceeds committed tunnel activity"),
        transaction.atomic(),
    ):
        ActivityCheckpoint.objects.create(
            tunnel=created.tunnel,
            credential=reader,
            last_event_position=created.tunnel.next_event_position,
        )

    assert Tunnel.objects.get(pk=created.tunnel.id).next_event_position == 3


def _pause_entry_after_tunnel_lock(*, credential: AgentCredential, address: str) -> Any:
    close_old_connections()
    try:
        from tunnels.entry import enter_tunnel

        return enter_tunnel(credential=credential, address=address)
    finally:
        close_old_connections()


def test_entry_and_reply_race_preserves_the_activity_boundary(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tunnels import entry
    from tunnels.activity import read_activity_once

    creator, _ = credential_factory(name="Entry race creator")
    created = _create_board(creator, key="entry-race-create")
    lock_obtained = threading.Event()
    release_entry = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    original_lock = entry._lock_entry_tunnel

    def blocking_lock(**kwargs: Any) -> Tunnel:
        tunnel = original_lock(**kwargs)
        if not lock_obtained.is_set():
            lock_obtained.set()
            assert release_entry.wait(timeout=15)
        return tunnel

    monkeypatch.setattr(entry, "_lock_entry_tunnel", blocking_lock)

    def post_during_entry() -> Any:
        close_old_connections()
        try:
            writer_started.set()
            return post_reply(
                credential=creator,
                idempotency_key="entry-race-reply",
                address=created.address,
                parent_id=created.root.id,
                content={"type": "text", "text": "After entry."},
            )
        finally:
            writer_finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        entering = executor.submit(
            _pause_entry_after_tunnel_lock,
            credential=creator,
            address=created.address,
        )
        assert lock_obtained.wait(timeout=15)
        writing = executor.submit(post_during_entry)
        assert writer_started.wait(timeout=15)
        assert not writer_finished.wait(timeout=0.2)
        release_entry.set()
        view = entering.result(timeout=30)
        posted = writing.result(timeout=30)

    assert view.snapshot_sequence == 1
    assert view.activity_position == 2
    page = read_activity_once(
        credential=creator,
        address=created.address,
        after_position=view.activity_position,
    )
    assert [event.message_id for event in page.events] == [posted.message.id]


def test_entry_and_cycle_close_race_returns_a_consistent_state(
    credential_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tunnels import entry

    creator, _ = credential_factory(name="Entry lifecycle race creator")
    created = _create_board(creator, key="entry-close-race-create")
    lock_obtained = threading.Event()
    release_entry = threading.Event()
    writer_started = threading.Event()
    writer_finished = threading.Event()
    original_lock = entry._lock_entry_tunnel

    def blocking_lock(**kwargs: Any) -> Tunnel:
        tunnel = original_lock(**kwargs)
        if not lock_obtained.is_set():
            lock_obtained.set()
            assert release_entry.wait(timeout=15)
        return tunnel

    monkeypatch.setattr(entry, "_lock_entry_tunnel", blocking_lock)

    def close_during_entry() -> None:
        close_old_connections()
        try:
            writer_started.set()
            close_cycle(
                credential=creator,
                idempotency_key="entry-close-race-close",
                address=created.address,
                expected_cycle_id=created.cycle.id,
            )
        finally:
            writer_finished.set()
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        entering = executor.submit(
            _pause_entry_after_tunnel_lock,
            credential=creator,
            address=created.address,
        )
        assert lock_obtained.wait(timeout=15)
        closing = executor.submit(close_during_entry)
        assert writer_started.wait(timeout=15)
        assert not writer_finished.wait(timeout=0.2)
        release_entry.set()
        view = entering.result(timeout=30)
        closing.result(timeout=30)

    assert view.cycle is not None and view.cycle.id == created.cycle.id
    assert view.root is not None and view.root.id == created.root.id
    assert view.activity_position == 2
