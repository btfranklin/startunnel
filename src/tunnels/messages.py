"""Immutable message creation and bounded tree reads."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from django.db import connection, transaction
from django.db.models import Count, Exists, OuterRef, Sum

from agents.models import AgentCredential
from core import limits as instance_limits
from core.audit import record_event
from core.errors import (
    CycleUnavailable,
    InvalidRequest,
    QuotaExceeded,
    TunnelUnavailable,
)
from core.idempotency import begin as begin_idempotency
from core.idempotency import finish as finish_idempotency
from core.limits import DEFAULT_LIMITS, MAX_CORRELATION_ID_CHARACTERS, MAX_SESSION_CHARACTERS
from core.logging import current_request_id
from core.metrics import MESSAGES_SENT

from .access import (
    active_cycle,
    cycle_is_readable,
    resolve_tunnel,
    select_cycle,
)
from .codec import canonical_address
from .events import append_events
from .models import (
    Cycle,
    Message,
    MessageMention,
    Tunnel,
    TunnelEvent,
)
from .payloads import ValidatedPayload, validate_content, validate_storable_text


@dataclass(frozen=True, slots=True)
class PostedReply:
    message: Message
    event_position: int
    replay: bool = False


@dataclass(frozen=True, slots=True)
class TreePage:
    tunnel: Tunnel
    cycle: Cycle
    messages: tuple[Message, ...]
    snapshot_sequence: int
    next_after_sequence: int | None
    complete: bool
    message_count: int
    content_bytes: int


@dataclass(frozen=True, slots=True)
class SubtreePage:
    tunnel: Tunnel
    cycle: Cycle
    root: Message
    messages: tuple[Message, ...]
    snapshot_sequence: int
    next_after_sequence: int | None
    truncated: bool


@dataclass(frozen=True, slots=True)
class ReplyPage:
    tunnel: Tunnel
    cycle: Cycle
    parent: Message
    messages: tuple[Message, ...]
    snapshot_sequence: int
    next_after_sequence: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class LeafPage:
    tunnel: Tunnel
    cycle: Cycle
    messages: tuple[Message, ...]
    snapshot_sequence: int
    next_after_sequence: int | None
    complete: bool


def _normalize_correlation_id(value: str | None) -> str:
    correlation = (value or "").strip()
    validate_storable_text(correlation, field="correlation_id")
    if len(correlation) > MAX_CORRELATION_ID_CHARACTERS:
        raise InvalidRequest(
            f"correlation_id can contain at most {MAX_CORRELATION_ID_CHARACTERS} characters."
        )
    return correlation


def _validate_session(value: str | None) -> str | None:
    if value is None:
        return None
    validate_storable_text(value, field="session")
    if (
        not 1 <= len(value) <= MAX_SESSION_CHARACTERS
        or value != value.strip()
        or any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
    ):
        raise InvalidRequest(
            f"session must contain 1 to {MAX_SESSION_CHARACTERS} characters "
            "with no outer whitespace "
            "or control characters."
        )
    return value


def _add_session_request_data(request_data: dict[str, object], session: str | None) -> None:
    # Preserve fingerprints from requests stored before sessions existed.
    if session is not None:
        request_data["session"] = session


def _create_message(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    sender: AgentCredential,
    parent: Message | None,
    sequence: int,
    payload: ValidatedPayload,
    correlation_id: str,
    session: str | None = None,
) -> Message:
    return Message.objects.create(
        tunnel=tunnel,
        cycle=cycle,
        parent=parent,
        sender=sender,
        sender_name=sender.name,
        session=session,
        sequence=sequence,
        depth=0 if parent is None else parent.depth + 1,
        payload_type=payload.payload_type,
        text_payload=payload.text,
        json_payload=payload.json_text,
        byte_count=payload.byte_count,
        correlation_id=correlation_id,
    )


def _resolve_mentions(*, mention_ids: list[UUID], maximum: int) -> list[AgentCredential]:
    _validate_mention_ids(mention_ids, maximum=maximum)
    if not mention_ids:
        return []
    rows = list(AgentCredential.objects.available().filter(id__in=mention_ids).order_by("id"))
    if len(rows) != len(mention_ids):
        raise InvalidRequest("One mentioned agent is not available.")
    return rows


def _validate_mention_ids(mention_ids: list[UUID], *, maximum: int) -> None:
    if len(mention_ids) > maximum:
        raise QuotaExceeded("This message has too many mentions.")
    if len(set(mention_ids)) != len(mention_ids):
        raise InvalidRequest("A message cannot mention one agent more than once.")


def _store_mentions(message: Message, mentions: list[AgentCredential]) -> None:
    MessageMention.objects.bulk_create(
        [
            MessageMention(message=message, credential=mentioned, display_name=mentioned.name)
            for mentioned in mentions
        ]
    )


@transaction.atomic
def post_reply(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    parent_id: UUID,
    content: Mapping[str, object],
    mentions: list[UUID] | None = None,
    correlation_id: str | None = None,
    session: str | None = None,
) -> PostedReply:
    limits = instance_limits.effective_limits()
    payload = validate_content(content, maximum_bytes=limits.bytes_per_message)
    correlation = _normalize_correlation_id(correlation_id)
    session = _validate_session(session)
    canonical = canonical_address(address)
    mention_ids = mentions or []
    request_data: dict[str, object] = {
        "address": canonical,
        "parent_id": str(parent_id),
        "content": content,
        "mentions": [str(item) for item in mention_ids],
        "correlation_id": correlation,
    }
    _add_session_request_data(request_data, session)
    idempotency = begin_idempotency(
        credential=credential,
        operation="post_reply",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        if not idempotency.record.resource_id:
            raise TunnelUnavailable()
        tunnel = resolve_tunnel(credential=credential, address=address)
        message = (
            Message.objects.select_related("cycle")
            .filter(pk=idempotency.record.resource_id, tunnel=tunnel)
            .first()
        )
        if message is None:
            raise TunnelUnavailable()
        if not cycle_is_readable(message.cycle):
            raise CycleUnavailable()
        event_position = idempotency.record.response.get("event_position")
        if (
            not isinstance(event_position, int)
            or isinstance(event_position, bool)
            or event_position < 1
        ):
            raise TunnelUnavailable()
        return PostedReply(message, event_position, replay=True)

    tunnel = resolve_tunnel(
        credential=credential,
        address=address,
        for_update=True,
    )
    cycle = active_cycle(tunnel=tunnel, for_update=True)
    parent = (
        Message.objects.select_for_update().filter(pk=parent_id, tunnel=tunnel, cycle=cycle).first()
    )
    if parent is None:
        raise InvalidRequest("The parent message is not in the active cycle.")
    if parent.depth + 1 > limits.maximum_tree_depth:
        raise QuotaExceeded("This reply would exceed the maximum tree depth.")
    if cycle.message_count >= limits.messages_per_cycle:
        raise QuotaExceeded("This cycle has reached its message limit.")
    if cycle.content_bytes + payload.byte_count > limits.content_bytes_per_cycle:
        raise QuotaExceeded("This cycle has reached its content-byte limit.")
    resolved_mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits.mentions_per_message,
    )
    sequence = cycle.next_sequence
    message = _create_message(
        tunnel=tunnel,
        cycle=cycle,
        sender=credential,
        parent=parent,
        sequence=sequence,
        payload=payload,
        correlation_id=correlation,
        session=session,
    )
    _store_mentions(message, resolved_mentions)
    cycle.next_sequence = sequence + 1
    cycle.message_count += 1
    cycle.content_bytes += message.byte_count
    cycle.save(update_fields=["next_sequence", "message_count", "content_bytes"])
    event_position = append_events(
        tunnel=tunnel,
        events=[
            TunnelEvent(cycle=cycle, message=message, event_type=TunnelEvent.Type.MESSAGE_POSTED)
        ],
    )
    from .beacons import note_beacon_response

    note_beacon_response(cycle=cycle, message=message)
    finish_idempotency(
        idempotency.record,
        response={
            "message_id": str(message.id),
            "cycle_id": str(cycle.id),
            "sequence": message.sequence,
            "event_position": event_position,
        },
        resource_id=str(message.id),
    )
    record_event(
        actor_credential=credential,
        action="message.posted",
        target_type="message",
        target_id=message.id,
        request_id=current_request_id(),
        metadata={"type": message.payload_type, "bytes": message.byte_count},
    )
    MESSAGES_SENT.labels(type=message.payload_type).inc()
    return PostedReply(message, event_position)


def read_tree(
    *,
    credential: AgentCredential,
    address: str,
    cycle_id: UUID | None = None,
    limit: int = DEFAULT_LIMITS.maximum_list_page,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> TreePage:
    limits = instance_limits.effective_limits()
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if after_sequence < 0:
        raise InvalidRequest("after_sequence cannot be negative.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    current_high_water = max(cycle.next_sequence - 1, 0)
    high_water = current_high_water if snapshot_sequence is None else snapshot_sequence
    if high_water < 1 or high_water > current_high_water:
        raise InvalidRequest("The snapshot sequence is not valid for this cycle.")
    rows = list(
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(cycle=cycle, sequence__gt=after_sequence, sequence__lte=high_water)
        .order_by("sequence", "id")[: limit + 1]
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    if high_water == current_high_water:
        message_count, content_bytes = cycle.message_count, cycle.content_bytes
    else:
        aggregates = Message.objects.filter(cycle=cycle, sequence__lte=high_water).aggregate(
            count=Count("id"), bytes=Sum("byte_count")
        )
        message_count, content_bytes = aggregates["count"] or 0, aggregates["bytes"] or 0
    return TreePage(
        tunnel=tunnel,
        cycle=cycle,
        messages=tuple(page),
        snapshot_sequence=high_water,
        next_after_sequence=page[-1].sequence if has_more and page else None,
        complete=not has_more,
        message_count=message_count,
        content_bytes=content_bytes,
    )


def get_message(
    *,
    credential: AgentCredential,
    address: str,
    message_id: UUID,
    cycle_id: UUID | None = None,
) -> Message:
    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    message = (
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(pk=message_id, tunnel=tunnel, cycle=cycle)
        .first()
    )
    if message is None:
        raise CycleUnavailable()
    return message


def get_branch(
    *,
    credential: AgentCredential,
    address: str,
    message_id: UUID,
    cycle_id: UUID | None = None,
) -> tuple[Message, ...]:
    focus = get_message(
        credential=credential,
        address=address,
        message_id=message_id,
        cycle_id=cycle_id,
    )
    return load_branch(focus)


def load_branch(focus: Message) -> tuple[Message, ...]:
    """Load the root-to-focus branch after the caller checks access."""

    table = connection.ops.quote_name(Message._meta.db_table)
    pk = Message._meta.pk
    with connection.cursor() as cursor:
        # Only the quoted model table name is interpolated; values use parameters.
        cursor.execute(
            f"""
            WITH RECURSIVE branch AS (
                SELECT id, parent_id, sequence FROM {table}
                WHERE id = %s AND cycle_id = %s
                UNION ALL
                SELECT parent.id, parent.parent_id, parent.sequence
                FROM {table} AS parent
                JOIN branch ON parent.id = branch.parent_id
                WHERE parent.cycle_id = %s AND parent.sequence < branch.sequence
            )
            SELECT id FROM branch ORDER BY sequence
            """,  # nosec B608
            [
                pk.get_db_prep_value(focus.id, connection),
                pk.get_db_prep_value(focus.cycle_id, connection),
                pk.get_db_prep_value(focus.cycle_id, connection),
            ],
        )
        rows = cursor.fetchall()
    if not rows:
        raise CycleUnavailable()
    return _messages_by_id_in_order([UUID(str(row[0])) for row in rows])


def list_replies(
    *,
    credential: AgentCredential,
    address: str,
    message_id: UUID,
    cycle_id: UUID | None = None,
    limit: int = DEFAULT_LIMITS.default_list_page,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> ReplyPage:
    parent = get_message(
        credential=credential,
        address=address,
        message_id=message_id,
        cycle_id=cycle_id,
    )
    limits = instance_limits.effective_limits()
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if after_sequence < 0:
        raise InvalidRequest("after_sequence cannot be negative.")
    current_high_water = max(parent.cycle.next_sequence - 1, 0)
    high_water = current_high_water if snapshot_sequence is None else snapshot_sequence
    if high_water < parent.sequence or high_water > current_high_water:
        raise InvalidRequest("The snapshot sequence is not valid for this cycle.")
    if (
        after_sequence
        and not Message.objects.filter(
            cycle=parent.cycle,
            parent=parent,
            sequence=after_sequence,
            sequence__lte=high_water,
        ).exists()
    ):
        raise InvalidRequest("The reply page position is not valid.")
    rows = list(
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(
            cycle=parent.cycle,
            parent=parent,
            sequence__gt=after_sequence,
            sequence__lte=high_water,
        )
        .order_by("sequence", "id")[: limit + 1]
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return ReplyPage(
        tunnel=parent.tunnel,
        cycle=parent.cycle,
        parent=parent,
        messages=tuple(page),
        snapshot_sequence=high_water,
        next_after_sequence=page[-1].sequence if has_more and page else None,
        complete=not has_more,
    )


def _messages_by_id_in_order(ids: list[UUID]) -> tuple[Message, ...]:
    rows = {
        row.id: row
        for row in Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(id__in=ids)
    }
    if any(message_id not in rows for message_id in ids):
        raise CycleUnavailable()
    return tuple(rows[message_id] for message_id in ids)


def _postgresql_subtree_page(
    *,
    cycle: Cycle,
    root: Message,
    max_depth: int,
    limit: int,
    after_sequence: int,
    high_water: int,
) -> tuple[tuple[Message, ...], bool, bool]:
    """Return one payload-bounded PostgreSQL subtree page and its state."""

    table = connection.ops.quote_name(Message._meta.db_table)
    query = f"""
        WITH RECURSIVE subtree AS (
            SELECT id, parent_id, sequence, 0 AS relative_depth,
                   ARRAY[sequence]::bigint[] AS traversal_path
            FROM {table}
            WHERE id = %s AND cycle_id = %s AND sequence <= %s
            UNION ALL
            SELECT child.id, child.parent_id, child.sequence,
                   subtree.relative_depth + 1,
                   subtree.traversal_path || child.sequence
            FROM {table} AS child
            JOIN subtree ON child.parent_id = subtree.id
            WHERE child.cycle_id = %s
              AND child.sequence <= %s
              AND subtree.relative_depth < %s
        ),
        page_state AS (
            SELECT
                (%s = 0 OR EXISTS (
                    SELECT 1 FROM subtree WHERE sequence = %s
                )) AS cursor_valid,
                EXISTS (
                    SELECT 1
                    FROM subtree
                    WHERE relative_depth = %s
                      AND EXISTS (
                          SELECT 1 FROM {table} AS child
                          WHERE child.parent_id = subtree.id
                            AND child.cycle_id = %s
                            AND child.sequence <= %s
                      )
                ) AS depth_truncated
        ),
        selected AS (
            SELECT subtree.id, subtree.sequence, subtree.traversal_path
            FROM subtree, page_state
            WHERE page_state.cursor_valid
              AND (
                  %s = 0
                  OR subtree.traversal_path > (
                      SELECT traversal_path FROM subtree WHERE sequence = %s
                  )
              )
            ORDER BY subtree.traversal_path
            LIMIT %s
        )
        SELECT NULL::uuid, NULL::bigint, cursor_valid, depth_truncated, TRUE,
               NULL::bigint[]
        FROM page_state
        UNION ALL
        SELECT selected.id, selected.sequence, TRUE, FALSE, FALSE,
               selected.traversal_path
        FROM selected
        ORDER BY 5 DESC, 6 NULLS FIRST
    """
    parameters: list[UUID | int] = [
        root.id,
        cycle.id,
        high_water,
        cycle.id,
        high_water,
        max_depth,
        after_sequence,
        after_sequence,
        max_depth,
        cycle.id,
        high_water,
        after_sequence,
        after_sequence,
        limit + 1,
    ]
    with connection.cursor() as cursor:
        cursor.execute(query, parameters)
        result = cursor.fetchall()
    cursor_valid = bool(result[0][2])
    depth_truncated = bool(result[0][3])
    if not cursor_valid:
        if not Message.objects.filter(pk=root.id, cycle=cycle).exists():
            raise CycleUnavailable()
        raise InvalidRequest("The subtree page position is not valid.")
    ids = [row[0] for row in result[1:]]
    if not ids and after_sequence == 0:
        raise CycleUnavailable()
    has_more = len(ids) > limit
    return _messages_by_id_in_order(ids[:limit]), has_more, depth_truncated


def get_subtree(
    *,
    credential: AgentCredential,
    address: str,
    message_id: UUID,
    cycle_id: UUID | None = None,
    max_depth: int = 8,
    limit: int = 200,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> SubtreePage:
    """Return one stable preorder page rooted at a selected message."""

    limits = instance_limits.effective_limits()
    if max_depth < 0 or max_depth > limits.maximum_tree_depth:
        raise InvalidRequest(f"max_depth must be from 0 through {limits.maximum_tree_depth}.")
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if after_sequence < 0:
        raise InvalidRequest("after_sequence cannot be negative.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    current_high_water = max(cycle.next_sequence - 1, 0)
    high_water = current_high_water if snapshot_sequence is None else snapshot_sequence
    if high_water < 1 or high_water > current_high_water:
        raise InvalidRequest("The snapshot sequence is not valid for this cycle.")
    root = (
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(pk=message_id, tunnel=tunnel, cycle=cycle)
        .first()
    )
    if root is None or root.sequence > high_water:
        raise CycleUnavailable()
    if connection.vendor == "postgresql":
        page_messages, has_more, depth_truncated = _postgresql_subtree_page(
            cycle=cycle,
            root=root,
            max_depth=max_depth,
            limit=limit,
            after_sequence=after_sequence,
            high_water=high_water,
        )
        return SubtreePage(
            tunnel=tunnel,
            cycle=cycle,
            root=root,
            messages=page_messages,
            snapshot_sequence=high_water,
            next_after_sequence=(
                page_messages[-1].sequence if has_more and page_messages else None
            ),
            truncated=depth_truncated or has_more,
        )

    # SQLite is used for fast unit tests. PostgreSQL uses the payload-bounded
    # recursive query above in every deployed environment.
    rows = list(
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(cycle=cycle, sequence__lte=high_water)
        .order_by("sequence", "id")
    )
    root = next((row for row in rows if row.id == message_id), None)
    if root is None:
        raise CycleUnavailable()
    children: dict[UUID, list[Message]] = {}
    for row in rows:
        if row.parent_id is not None:
            children.setdefault(row.parent_id, []).append(row)

    preorder: list[Message] = []
    depth_truncated = False
    stack: list[tuple[Message, int]] = [(root, 0)]
    while stack:
        message, relative_depth = stack.pop()
        preorder.append(message)
        direct_children = children.get(message.id, [])
        if relative_depth >= max_depth:
            depth_truncated = depth_truncated or bool(direct_children)
            continue
        stack.extend((child, relative_depth + 1) for child in reversed(direct_children))

    start = 0
    if after_sequence:
        try:
            start = next(
                index + 1
                for index, message in enumerate(preorder)
                if message.sequence == after_sequence
            )
        except StopIteration as error:
            raise InvalidRequest("The subtree page position is not valid.") from error
    remaining = preorder[start:]
    has_more = len(remaining) > limit
    page = remaining[:limit]
    return SubtreePage(
        tunnel=tunnel,
        cycle=cycle,
        root=root,
        messages=tuple(page),
        snapshot_sequence=high_water,
        next_after_sequence=page[-1].sequence if has_more and page else None,
        truncated=depth_truncated or has_more,
    )


def list_leaves(
    *,
    credential: AgentCredential,
    address: str,
    cycle_id: UUID | None = None,
    limit: int = DEFAULT_LIMITS.default_list_page,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> LeafPage:
    """Return a stable sequence-ordered page of messages with no snapshot child."""

    limits = instance_limits.effective_limits()
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    if after_sequence < 0:
        raise InvalidRequest("after_sequence cannot be negative.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    cycle = select_cycle(tunnel=tunnel, cycle_id=cycle_id)
    current_high_water = max(cycle.next_sequence - 1, 0)
    high_water = current_high_water if snapshot_sequence is None else snapshot_sequence
    if high_water < 1 or high_water > current_high_water:
        raise InvalidRequest("The snapshot sequence is not valid for this cycle.")
    eligible_children = Message.objects.filter(
        cycle=cycle,
        parent_id=OuterRef("pk"),
        sequence__lte=high_water,
    )
    rows = list(
        Message.objects.select_related("cycle")
        .prefetch_related("mentions")
        .filter(
            cycle=cycle,
            sequence__gt=after_sequence,
            sequence__lte=high_water,
        )
        .annotate(has_snapshot_child=Exists(eligible_children))
        .filter(has_snapshot_child=False)
        .order_by("sequence", "id")[: limit + 1]
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return LeafPage(
        tunnel=tunnel,
        cycle=cycle,
        messages=tuple(page),
        snapshot_sequence=high_water,
        next_after_sequence=page[-1].sequence if has_more and page else None,
        complete=not has_more,
    )
