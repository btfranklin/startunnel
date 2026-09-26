"""Participant summaries preserve immutable attribution and stable pages."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from tunnels.errors import CycleUnavailable, InvalidRequest
from tunnels.participants import list_participants
from tunnels.services import create_tunnel, post_reply

pytestmark = pytest.mark.django_db


def _create(credential: Any, *, key: str = "participant-create") -> Any:
    return create_tunnel(
        credential=credential,
        idempotency_key=key,
        root_content={"type": "text", "text": "Coordinate this work."},
    )


def _reply(
    credential: Any,
    created: Any,
    parent: Any,
    *,
    key: str,
    mentions: list[Any] | None = None,
) -> Any:
    return post_reply(
        credential=credential,
        idempotency_key=key,
        address=created.address,
        parent_id=parent.id,
        content={"type": "text", "text": key},
        mentions=[item.id for item in mentions or []],
    ).message


def test_participants_merge_sends_and_mentions_with_immutable_name_snapshots(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Coordinator")
    worker, _ = credential_factory(name="Worker at send")
    observer, _ = credential_factory(name="Observer at mention")
    created = _create(coordinator)
    worker_message = _reply(
        worker,
        created,
        created.root,
        key="participant-worker-send",
        mentions=[observer],
    )
    worker.name = "Worker after send"
    worker.save(update_fields=["name"])
    observer.name = "Observer after mention"
    observer.save(update_fields=["name"])
    _reply(
        coordinator,
        created,
        worker_message,
        key="participant-second-mention",
        mentions=[worker, observer],
    )

    page = list_participants(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
    )
    by_id = {item.credential_id: item for item in page.participants}

    assert [item.credential_id for item in page.participants] == [
        coordinator.id,
        *sorted((worker.id, observer.id), key=str),
    ]
    assert by_id[coordinator.id].message_count == 2
    assert by_id[coordinator.id].mention_count == 0
    assert by_id[worker.id].message_count == 1
    assert by_id[worker.id].mention_count == 1
    assert by_id[worker.id].name_snapshot == "Worker at send"
    assert by_id[observer.id].message_count == 0
    assert by_id[observer.id].mention_count == 2
    assert by_id[observer.id].name_snapshot == "Observer after mention"
    assert by_id[observer.id].first_sequence == worker_message.sequence
    assert by_id[observer.id].last_sequence == 3
    assert page.complete is True


def test_participant_pages_keep_one_snapshot_during_later_messages(
    credential_factory: Any,
) -> None:
    coordinator, _ = credential_factory(name="Page coordinator")
    first, _ = credential_factory(name="First participant")
    second, _ = credential_factory(name="Second participant")
    later, _ = credential_factory(name="Later participant")
    created = _create(coordinator, key="participant-page-create")
    _reply(first, created, created.root, key="participant-page-first")
    _reply(second, created, created.root, key="participant-page-second")

    first_page = list_participants(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        limit=2,
    )
    assert [item.credential_id for item in first_page.participants] == [
        coordinator.id,
        first.id,
    ]
    assert first_page.complete is False
    assert first_page.next_after_first_sequence is not None
    assert first_page.next_after_credential_id is not None

    _reply(later, created, created.root, key="participant-page-later")
    second_page = list_participants(
        credential=coordinator,
        address=created.address,
        cycle_id=created.cycle.id,
        snapshot_sequence=first_page.snapshot_sequence,
        after_first_sequence=first_page.next_after_first_sequence,
        after_credential_id=first_page.next_after_credential_id,
        limit=2,
    )
    assert [item.credential_id for item in second_page.participants] == [second.id]
    assert later.id not in {item.credential_id for item in second_page.participants}
    assert second_page.complete is True


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 1_001}, "limit"),
        ({"snapshot_sequence": 0}, "snapshot"),
        ({"snapshot_sequence": 2}, "snapshot"),
        ({"after_first_sequence": 1}, "incomplete"),
        ({"after_credential_id": uuid4()}, "incomplete"),
        (
            {"after_first_sequence": 0, "after_credential_id": uuid4()},
            "page position",
        ),
    ],
)
def test_participant_read_rejects_invalid_page_state(
    credential_factory: Any,
    arguments: dict[str, Any],
    message: str,
) -> None:
    credential, _ = credential_factory()
    created = _create(credential, key=f"participant-invalid-{uuid4()}")

    with pytest.raises(InvalidRequest, match=message):
        list_participants(
            credential=credential,
            address=created.address,
            cycle_id=created.cycle.id,
            **arguments,
        )


def test_participant_read_keeps_cycle_scope(credential_factory: Any) -> None:
    personal, _ = credential_factory()
    first = _create(personal, key="participant-scope-first")
    second = _create(personal, key="participant-scope-second")
    with pytest.raises(CycleUnavailable):
        list_participants(
            credential=personal,
            address=first.address,
            cycle_id=second.cycle.id,
        )
