"""Deterministic, bounded context assembly for one cycle tree."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from agents.models import AgentCredential

from .access import limits_for, resolve_tunnel, select_cycle
from .errors import ContextBudgetTooSmall, InvalidRequest
from .message_data import message_payload
from .models import ActivityCheckpoint, Message, Tunnel, TunnelEvent
from .services import get_message, load_branch

ContextReason = Literal["ancestor", "focus", "direct_reply", "reference", "recent_activity"]


@dataclass(frozen=True, slots=True)
class ContextItem:
    reason: ContextReason
    message: Message
    wire_bytes: int


@dataclass(frozen=True, slots=True)
class ContextView:
    tunnel: Tunnel
    cycle_id: UUID
    items: tuple[ContextItem, ...]
    snapshot_sequence: int
    activity_position: int
    used_items: int
    used_bytes: int
    truncated: bool


def context_item_payload(message: Message, reason: ContextReason) -> dict[str, Any]:
    """Return the canonical public context item used for exact byte accounting."""

    return {"reason": reason, "message": message_payload(message)}


def _wire_bytes(message: Message, reason: ContextReason) -> int:
    return len(
        json.dumps(
            context_item_payload(message, reason),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def get_context(
    *,
    credential: AgentCredential,
    address: str,
    focus_message_id: UUID,
    cycle_id: UUID | None = None,
    snapshot_sequence: int | None = None,
    activity_position: int | None = None,
    include_branch: bool = True,
    include_direct_replies: bool = True,
    include_recent_activity: bool = True,
    referenced_message_ids: tuple[UUID, ...] = (),
    after_activity_position: int | None = None,
    max_items: int = 200,
    max_bytes: int = 1_048_576,
) -> ContextView:
    """Select one required branch and optional messages within exact wire budgets."""

    limits = limits_for(credential)
    if max_items < 1 or max_items > limits.maximum_context_items:
        raise InvalidRequest(f"max_items must be from 1 through {limits.maximum_context_items}.")
    if max_bytes < 1 or max_bytes > limits.maximum_context_bytes:
        raise InvalidRequest(f"max_bytes must be from 1 through {limits.maximum_context_bytes}.")
    if after_activity_position is not None and after_activity_position < 0:
        raise InvalidRequest("The activity position cannot be negative.")
    if len(referenced_message_ids) > 100:
        raise InvalidRequest("referenced_message_ids can contain at most 100 values.")
    if len(set(referenced_message_ids)) != len(referenced_message_ids):
        raise InvalidRequest("referenced_message_ids cannot contain a duplicate value.")

    tunnel = resolve_tunnel(credential=credential, address=address)
    if after_activity_position is None:
        after_activity_position = (
            ActivityCheckpoint.objects.filter(tunnel=tunnel, credential=credential)
            .values_list("last_event_position", flat=True)
            .first()
            or 0
        )
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    focus = get_message(
        credential=credential,
        address=address,
        message_id=focus_message_id,
        cycle_id=cycle.id,
    )
    current_sequence = max(cycle.next_sequence - 1, 0)
    sequence_high_water = current_sequence if snapshot_sequence is None else snapshot_sequence
    if sequence_high_water < focus.sequence or sequence_high_water > current_sequence:
        raise InvalidRequest("The context snapshot is not valid for this cycle.")
    current_activity = max(tunnel.next_event_position - 1, 0)
    activity_high_water = current_activity if activity_position is None else activity_position
    if activity_high_water < 0 or activity_high_water > current_activity:
        raise InvalidRequest("The context activity position is not valid for this tunnel.")
    if after_activity_position > activity_high_water:
        raise InvalidRequest("The activity position is not valid for this context snapshot.")

    if not include_branch and focus.parent_id is not None:
        raise InvalidRequest("A non-root context must include its complete branch.")
    branch = load_branch(focus) if include_branch else (focus,)
    required: list[ContextItem] = []
    for message in branch:
        reason: ContextReason = "focus" if message.id == focus.id else "ancestor"
        required.append(ContextItem(reason, message, _wire_bytes(message, reason)))
    required_bytes = sum(item.wire_bytes for item in required)
    if len(required) > max_items or required_bytes > max_bytes:
        raise ContextBudgetTooSmall()

    reference_rows: dict[UUID, Message] = {}
    if referenced_message_ids:
        reference_rows = {
            message.id: message
            for message in Message.objects.select_related("sender", "parent", "cycle")
            .prefetch_related("mentions__credential")
            .filter(
                id__in=referenced_message_ids,
                cycle=cycle,
                sequence__lte=sequence_high_water,
            )
        }
        if len(reference_rows) != len(referenced_message_ids):
            raise InvalidRequest("One referenced message is not in this context snapshot.")

    selected = list(required)
    selected_ids = {item.message.id for item in selected}
    used_bytes = required_bytes
    truncated = False

    def select_candidates(candidates: Iterable[tuple[ContextReason, Message]]) -> bool:
        nonlocal truncated, used_bytes
        for reason, message in candidates:
            if message.id in selected_ids:
                continue
            candidate_bytes = _wire_bytes(message, reason)
            if len(selected) >= max_items or used_bytes + candidate_bytes > max_bytes:
                truncated = True
                return False
            selected.append(ContextItem(reason, message, candidate_bytes))
            selected_ids.add(message.id)
            used_bytes += candidate_bytes
        return True

    continue_selection = True
    if include_direct_replies:
        direct_limit = max_items - len(selected) + 1
        direct_replies = (
            Message.objects.select_related("sender", "parent", "cycle")
            .prefetch_related("mentions__credential")
            .filter(cycle=cycle, parent=focus, sequence__lte=sequence_high_water)
            .exclude(id__in=selected_ids)
            .order_by("sequence", "id")[:direct_limit]
        )
        continue_selection = select_candidates(
            ("direct_reply", message) for message in direct_replies
        )
    if continue_selection and referenced_message_ids:
        continue_selection = select_candidates(
            ("reference", reference_rows[item]) for item in referenced_message_ids
        )
    if continue_selection and include_recent_activity:
        activity_limit = max_items - len(selected) + 1
        recent_events = (
            TunnelEvent.objects.select_related(
                "message", "message__sender", "message__parent", "message__cycle"
            )
            .prefetch_related("message__mentions__credential")
            .filter(
                tunnel=tunnel,
                cycle=cycle,
                event_type=TunnelEvent.Type.MESSAGE_POSTED,
                message__isnull=False,
                message__sequence__lte=sequence_high_water,
                position__gt=after_activity_position,
                position__lte=activity_high_water,
            )
            .exclude(message_id__in=selected_ids)
            .order_by("position", "id")[:activity_limit]
        )
        select_candidates(
            ("recent_activity", event.message)
            for event in recent_events
            if event.message is not None
        )

    confirmed = resolve_tunnel(credential=credential, address=address)
    if confirmed.id != tunnel.id:
        raise InvalidRequest("The tunnel changed during context assembly.")

    return ContextView(
        tunnel=tunnel,
        cycle_id=cycle.id,
        items=tuple(selected),
        snapshot_sequence=sequence_high_water,
        activity_position=activity_high_water,
        used_items=len(selected),
        used_bytes=used_bytes,
        truncated=truncated,
    )
