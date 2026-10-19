"""Consistent, bounded entry reads for one stable tunnel."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from agents.models import AgentCredential
from core.cursors import encode_cursor
from core.errors import EntryBudgetTooSmall, InvalidCredential, InvalidRequest

from .access import credential_is_active, limits_for, resolve_tunnel
from .message_data import cycle_payload, message_payload
from .models import Cycle, Message, Tunnel


@dataclass(frozen=True, slots=True)
class TunnelEntry:
    tunnel: Tunnel
    cycle: Cycle | None
    root: Message | None
    recent_messages: tuple[Message, ...]
    snapshot_sequence: int | None
    activity_position: int
    history_upper_sequence: int | None
    history_omitted: bool
    history_cursor: str | None
    activity_cursor: str


def _entry_message_payload(message: Message) -> dict[str, Any]:
    payload = message_payload(message)
    # Keep datetime precision for Pydantic. Its output then matches the shared
    # JSON encoder's millisecond format, including timestamps with zero millis.
    payload["created_at"] = message.created_at
    return payload


def _entry_payload(entry: TunnelEntry, *, include_recent_messages: bool) -> dict[str, Any]:
    return {
        "status": entry.tunnel.state,
        "cycle": cycle_payload(entry.cycle) if entry.cycle is not None else None,
        "root": _entry_message_payload(entry.root) if entry.root is not None else None,
        "recent_messages": (
            [_entry_message_payload(message) for message in entry.recent_messages]
            if include_recent_messages
            else []
        ),
        "snapshot_sequence": entry.snapshot_sequence,
        "history_omitted": entry.history_omitted,
        "history_cursor": entry.history_cursor,
        "activity_cursor": entry.activity_cursor,
    }


def entry_payload(entry: TunnelEntry) -> dict[str, Any]:
    """Return the public entry payload used for exact UTF-8 byte accounting."""

    return _entry_payload(entry, include_recent_messages=True)


def _message_wire_size(message: Message) -> int:
    return len(json.dumps(message_payload(message), cls=DjangoJSONEncoder).encode("utf-8"))


def _response_wire_size(
    entry: TunnelEntry, *, fixed_size: int, message_bytes: int, message_count: int
) -> int:
    # The fixed payload contains an empty list, a null cursor, and false.
    cursor_bytes = len(json.dumps(entry.history_cursor).encode("utf-8")) - len("null")
    list_bytes = message_bytes + max(message_count - 1, 0) * 2
    return fixed_size + cursor_bytes + list_bytes - int(entry.history_omitted)


def _make_entry(
    *,
    tunnel: Tunnel,
    cycle: Cycle | None,
    root: Message | None,
    recent_messages: tuple[Message, ...],
    snapshot_sequence: int | None,
    activity_position: int,
    activity_cursor: str,
) -> TunnelEntry:
    history_upper_sequence = None
    history_omitted = False
    history_cursor = None
    if root is not None and cycle is not None and snapshot_sequence is not None:
        history_upper_sequence = (
            recent_messages[0].sequence - 1 if recent_messages else snapshot_sequence
        )
        history_omitted = history_upper_sequence > root.sequence
        if history_omitted:
            history_cursor = encode_cursor(
                "tree-page",
                tunnel_id=str(tunnel.id),
                cycle_id=str(cycle.id),
                snapshot_sequence=history_upper_sequence,
                after_sequence=root.sequence,
            )
    return TunnelEntry(
        tunnel=tunnel,
        cycle=cycle,
        root=root,
        recent_messages=recent_messages,
        snapshot_sequence=snapshot_sequence,
        activity_position=activity_position,
        history_upper_sequence=history_upper_sequence,
        history_omitted=history_omitted,
        history_cursor=history_cursor,
        activity_cursor=activity_cursor,
    )


@transaction.atomic
def enter_tunnel(
    *, credential: AgentCredential, address: str, limit: int = 20, max_bytes: int = 262_144
) -> TunnelEntry:
    """Read a count- and byte-bounded snapshot while tunnel writes wait on its row lock."""

    limits = limits_for(credential)
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if max_bytes < 1 or max_bytes > limits.maximum_context_bytes:
        raise InvalidRequest(f"max_bytes must be from 1 through {limits.maximum_context_bytes}.")

    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    cycle = None
    root = None
    candidates: tuple[Message, ...] = ()
    snapshot_sequence = None
    activity_position = max(tunnel.next_event_position - 1, 0)

    if tunnel.state == Tunnel.State.ACTIVE:
        cycle = (
            Cycle.objects.select_for_update()
            .filter(
                tunnel=tunnel,
                state=Cycle.State.ACTIVE,
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if cycle is not None:
            snapshot_sequence = max(cycle.next_sequence - 1, 0)
            root = (
                Message.objects.select_related("cycle")
                .prefetch_related("mentions")
                .get(cycle=cycle, sequence=1)
            )
            candidate_limit = limit - 1
            if candidate_limit:
                rows = list(
                    Message.objects.select_related("cycle")
                    .prefetch_related("mentions")
                    .filter(
                        cycle=cycle,
                        sequence__gt=root.sequence,
                        sequence__lte=snapshot_sequence,
                    )
                    .order_by("-sequence", "-id")[:candidate_limit]
                )
                rows.reverse()
                candidates = tuple(rows)

    activity_cursor = encode_cursor(
        "activity", tunnel_id=str(tunnel.id), position=activity_position
    )
    result = _make_entry(
        tunnel=tunnel,
        cycle=cycle,
        root=root,
        recent_messages=(),
        snapshot_sequence=snapshot_sequence,
        activity_position=activity_position,
        activity_cursor=activity_cursor,
    )
    fixed_payload = _entry_payload(result, include_recent_messages=False)
    fixed_payload.update(history_cursor=None, history_omitted=False)
    fixed_size = len(json.dumps(fixed_payload, cls=DjangoJSONEncoder).encode("utf-8"))
    chosen: list[Message] = []
    message_bytes = 0
    for message in reversed(candidates):
        # Only the oldest selected sequence changes the history cursor. Its
        # compressed length can change at each boundary, so measure it exactly.
        trial = _make_entry(
            tunnel=tunnel,
            cycle=cycle,
            root=root,
            recent_messages=(message,),
            snapshot_sequence=snapshot_sequence,
            activity_position=activity_position,
            activity_cursor=activity_cursor,
        )
        trial_bytes = message_bytes + _message_wire_size(message)
        if (
            _response_wire_size(
                trial,
                fixed_size=fixed_size,
                message_bytes=trial_bytes,
                message_count=len(chosen) + 1,
            )
            > max_bytes
        ):
            break
        chosen.append(message)
        message_bytes = trial_bytes
        result = trial

    result = replace(result, recent_messages=tuple(reversed(chosen)))
    if (
        _response_wire_size(
            result,
            fixed_size=fixed_size,
            message_bytes=message_bytes,
            message_count=len(chosen),
        )
        > max_bytes
    ):
        raise EntryBudgetTooSmall()
    if not credential_is_active(credential):
        raise InvalidCredential()
    return result
