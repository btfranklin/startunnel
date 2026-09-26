"""Cycle participant summaries from immutable message attribution.

A participant summary describes recorded sends and mentions. It does not
describe presence, availability, authority, or a real-world identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.db.models import Count, Max, Min, OuterRef, Subquery

from agents.models import AgentCredential

from .access import limits_for, resolve_tunnel, select_cycle
from .errors import InvalidRequest
from .models import Cycle, Message, MessageMention, Tunnel


@dataclass(frozen=True, slots=True)
class ParticipantSummary:
    credential_id: UUID
    name_snapshot: str
    message_count: int
    mention_count: int
    first_sequence: int
    last_sequence: int


@dataclass(frozen=True, slots=True)
class ParticipantPage:
    tunnel: Tunnel
    cycle: Cycle
    participants: tuple[ParticipantSummary, ...]
    snapshot_sequence: int
    next_after_first_sequence: int | None
    next_after_credential_id: UUID | None
    complete: bool


def list_participants(
    *,
    credential: AgentCredential,
    address: str,
    cycle_id: UUID | None = None,
    snapshot_sequence: int | None = None,
    after_first_sequence: int | None = None,
    after_credential_id: UUID | None = None,
    limit: int = 100,
) -> ParticipantPage:
    """List recorded cycle participants in stable first-occurrence order."""

    limits = limits_for(credential)
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if (after_first_sequence is None) != (after_credential_id is None):
        raise InvalidRequest("The participant page position is incomplete.")
    if after_first_sequence is not None and after_first_sequence < 1:
        raise InvalidRequest("The participant page position is not valid.")

    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    current_sequence = max(cycle.next_sequence - 1, 0)
    high_water = current_sequence if snapshot_sequence is None else snapshot_sequence
    if high_water < 1 or high_water > current_sequence:
        raise InvalidRequest("The participant snapshot is not valid for this cycle.")

    latest_sender_name = (
        Message.objects.filter(
            cycle=cycle,
            sequence__lte=high_water,
            sender_id=OuterRef("sender_id"),
        )
        .order_by("-sequence", "-id")
        .values("sender_name")[:1]
    )
    sender_rows = (
        Message.objects.filter(cycle=cycle, sequence__lte=high_water)
        .values("sender_id")
        .annotate(
            message_count=Count("id"),
            first_sequence=Min("sequence"),
            last_sequence=Max("sequence"),
            name_snapshot=Subquery(latest_sender_name),
        )
    )

    latest_mention_name = (
        MessageMention.objects.filter(
            message__cycle=cycle,
            message__sequence__lte=high_water,
            credential_id=OuterRef("credential_id"),
        )
        .order_by("-message__sequence", "-id")
        .values("display_name")[:1]
    )
    mention_rows = (
        MessageMention.objects.filter(
            message__cycle=cycle,
            message__sequence__lte=high_water,
        )
        .values("credential_id")
        .annotate(
            mention_count=Count("id"),
            first_sequence=Min("message__sequence"),
            last_sequence=Max("message__sequence"),
            name_snapshot=Subquery(latest_mention_name),
        )
    )

    merged: dict[UUID, ParticipantSummary] = {}
    for sender_row in sender_rows:
        credential_id = sender_row["sender_id"]
        merged[credential_id] = ParticipantSummary(
            credential_id=credential_id,
            name_snapshot=sender_row["name_snapshot"],
            message_count=sender_row["message_count"],
            mention_count=0,
            first_sequence=sender_row["first_sequence"],
            last_sequence=sender_row["last_sequence"],
        )
    for mention_row in mention_rows:
        credential_id = mention_row["credential_id"]
        existing = merged.get(credential_id)
        if existing is None:
            merged[credential_id] = ParticipantSummary(
                credential_id=credential_id,
                name_snapshot=mention_row["name_snapshot"],
                message_count=0,
                mention_count=mention_row["mention_count"],
                first_sequence=mention_row["first_sequence"],
                last_sequence=mention_row["last_sequence"],
            )
            continue
        mention_last = mention_row["last_sequence"]
        merged[credential_id] = ParticipantSummary(
            credential_id=credential_id,
            name_snapshot=existing.name_snapshot,
            message_count=existing.message_count,
            mention_count=mention_row["mention_count"],
            first_sequence=min(existing.first_sequence, mention_row["first_sequence"]),
            last_sequence=max(existing.last_sequence, mention_last),
        )

    rows = sorted(merged.values(), key=lambda item: (item.first_sequence, str(item.credential_id)))
    if after_first_sequence is not None and after_credential_id is not None:
        after = (after_first_sequence, str(after_credential_id))
        rows = [item for item in rows if (item.first_sequence, str(item.credential_id)) > after]
    page_rows = rows[: limit + 1]
    has_more = len(page_rows) > limit
    page = tuple(page_rows[:limit])
    confirmed = resolve_tunnel(credential=credential, address=address)
    if confirmed.id != tunnel.id:
        raise InvalidRequest("The tunnel changed during the participant read.")
    return ParticipantPage(
        tunnel=tunnel,
        cycle=cycle,
        participants=page,
        snapshot_sequence=high_water,
        next_after_first_sequence=page[-1].first_sequence if has_more and page else None,
        next_after_credential_id=page[-1].credential_id if has_more and page else None,
        complete=not has_more,
    )
