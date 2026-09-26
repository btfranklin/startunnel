"""Transactional services for stable tunnels and immutable cycle trees.

PostgreSQL owns tree, lifecycle, rate-limit, and notification correctness.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.models import Count, Exists, OuterRef, Q, Sum
from django.utils import timezone

from accounts.models import User
from agents.models import AgentCredential
from core.logging import current_request_id
from core.metrics import MESSAGES_SENT, TUNNELS_CREATED
from core.notifications import ACTIVITY_CHANNEL, notify, notify_maintenance

from .access import (
    active_cycle,
    credential_is_active,
    cycle_is_readable,
    limits_for,
    resolve_tunnel,
    select_cycle,
)
from .codec import (
    address_digest,
    derive_token,
    display_address,
    encode_token,
    parse_address,
)
from .errors import (
    CycleUnavailable,
    InvalidCredential,
    InvalidRequest,
    LifecycleConflict,
    QuotaExceeded,
    TunnelUnavailable,
)
from .idempotency import begin as begin_idempotency
from .idempotency import find_replay as find_idempotency_replay
from .idempotency import finish as finish_idempotency
from .models import (
    AuditEvent,
    Cycle,
    IdempotencyRecord,
    Message,
    MessageMention,
    Tunnel,
    TunnelAddress,
    TunnelEvent,
)
from .payloads import ValidatedPayload, validate_content, validate_storable_text

DEFAULT_CYCLE_SECONDS = 3_600
MAX_CYCLE_SECONDS = 604_800
MIN_CYCLE_SECONDS = 300
ADDRESS_ALLOCATION_ATTEMPTS = 8
INSTANCE_TUNNEL_QUOTA_LOCK = 7_195_415_308_902


@dataclass(frozen=True, slots=True)
class CreatedTunnel:
    tunnel: Tunnel
    address: str
    display_address: str
    cycle: Cycle
    root: Message
    tunnel_state: str
    cycle_state: str
    message_count: int
    content_bytes: int
    event_position: int
    replay: bool = False


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


@dataclass(frozen=True, slots=True)
class AgentTunnelStatus:
    tunnels: int


@dataclass(frozen=True, slots=True)
class TunnelStatus:
    tunnel: Tunnel
    current_address: TunnelAddress
    current_cycle: Cycle | None


@dataclass(frozen=True, slots=True)
class CyclePage:
    tunnel: Tunnel
    cycles: tuple[Cycle, ...]
    snapshot_number: int
    next_before_number: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class StartedCycle:
    tunnel: Tunnel
    cycle: Cycle
    root: Message
    cycle_state: str
    message_count: int
    content_bytes: int
    event_position: int
    replay: bool = False


@dataclass(frozen=True, slots=True)
class RotatedAddress:
    tunnel: Tunnel
    address: str
    display_address: str
    generation: int


def _lock_instance_tunnel_quota() -> None:
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [INSTANCE_TUNNEL_QUOTA_LOCK])


def _cycle_lifetime(expires_in_seconds: int | None) -> int:
    value = DEFAULT_CYCLE_SECONDS if expires_in_seconds is None else expires_in_seconds
    if value < MIN_CYCLE_SECONDS or value > MAX_CYCLE_SECONDS:
        raise InvalidRequest(
            f"expires_in_seconds must be from {MIN_CYCLE_SECONDS} through {MAX_CYCLE_SECONDS}."
        )
    return value


def agent_tunnel_status(*, credential: AgentCredential) -> AgentTunnelStatus:
    if not credential_is_active(credential):
        raise InvalidCredential()
    return AgentTunnelStatus(
        tunnels=Tunnel.objects.exclude(state=Tunnel.State.RETIRED).count(),
    )


def _validate_label(value: str, *, field: str) -> str:
    normalized = value.strip()
    validate_storable_text(normalized, field=field)
    if len(normalized) > 160:
        raise InvalidRequest(f"{field} can contain at most 160 characters.")
    return normalized


def _content_digest(payload: ValidatedPayload) -> bytes:
    body = payload.text if payload.payload_type == Message.PayloadType.TEXT else payload.json_text
    assert body is not None
    return hashlib.sha256(f"{payload.payload_type}\x00{body}".encode()).digest()


def _address_token(*, record: IdempotencyRecord, credential: AgentCredential) -> bytes:
    nonce = bytes(record.derivation_nonce or b"")
    if len(nonce) != 32:
        raise TunnelUnavailable()
    return derive_token(
        credential_id=str(credential.id),
        operation="create_tunnel",
        idempotency_record_id=str(record.id),
        idempotency_key_digest=bytes(record.key_digest),
        derivation_nonce=nonce,
    )


def _allocate_address(
    *,
    tunnel: Tunnel,
    credential: AgentCredential,
    idempotency_record: IdempotencyRecord,
) -> str:
    for _attempt in range(ADDRESS_ALLOCATION_ATTEMPTS):
        idempotency_record.derivation_nonce = secrets.token_bytes(32)
        token = _address_token(
            record=idempotency_record,
            credential=credential,
        )
        address = encode_token(token)
        try:
            with transaction.atomic():
                TunnelAddress.objects.create(
                    tunnel=tunnel,
                    address_digest=address_digest(token),
                )
        except IntegrityError:
            continue
        idempotency_record.save(update_fields=["derivation_nonce"])
        return address
    raise InvalidRequest("StarTunnel could not allocate an address. Try the request again.")


def _allocate_random_address(*, tunnel: Tunnel, generation: int) -> tuple[TunnelAddress, str]:
    """Allocate one random current address inside the locked tunnel transaction."""

    for _attempt in range(ADDRESS_ALLOCATION_ATTEMPTS):
        address = encode_token(secrets.token_bytes(16))
        token = parse_address(address)
        try:
            with transaction.atomic():
                row = TunnelAddress.objects.create(
                    tunnel=tunnel,
                    address_digest=address_digest(token),
                    generation=generation,
                )
        except IntegrityError:
            continue
        return row, address
    raise InvalidRequest("StarTunnel could not allocate an address. Try the request again.")


def _create_message(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    sender: AgentCredential,
    parent: Message | None,
    sequence: int,
    payload: ValidatedPayload,
    correlation_id: str,
) -> Message:
    return Message.objects.create(
        tunnel=tunnel,
        cycle=cycle,
        parent=parent,
        sender=sender,
        sender_name=sender.name,
        sequence=sequence,
        depth=0 if parent is None else parent.depth + 1,
        payload_type=payload.payload_type,
        text_payload=payload.text,
        json_payload=payload.json_text,
        byte_count=payload.byte_count,
        content_digest=_content_digest(payload),
        correlation_id=correlation_id,
    )


def _resolve_mentions(*, mention_ids: list[UUID], maximum: int) -> list[AgentCredential]:
    _validate_mention_ids(mention_ids, maximum=maximum)
    if not mention_ids:
        return []
    query = AgentCredential.objects.filter(id__in=mention_ids)
    rows = list(query.order_by("id"))
    if len(rows) != len(mention_ids) or any(not credential_is_active(row) for row in rows):
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
def create_tunnel(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    root_content: dict[str, Any],
    label: str = "",
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
    new_request_gate: Callable[[], None] | None = None,
) -> CreatedTunnel:
    if not credential_is_active(credential):
        raise InvalidCredential()
    seconds = _cycle_lifetime(expires_in_seconds)
    tunnel_label = _validate_label(label, field="A tunnel label")
    normalized_cycle_label = _validate_label(cycle_label, field="A cycle label")
    limits = limits_for(credential)
    root_payload = validate_content(root_content, maximum_bytes=limits.bytes_per_message)
    correlation = (root_correlation_id or "").strip()
    validate_storable_text(correlation, field="correlation_id")
    if len(correlation) > 128:
        raise InvalidRequest("correlation_id can contain at most 128 characters.")
    mention_ids = root_mentions or []
    request_data = {
        "label": tunnel_label,
        "cycle_label": normalized_cycle_label,
        "expires_in_seconds": seconds,
        "root_content": root_content,
        "root_mentions": [str(item) for item in mention_ids],
        "root_correlation_id": correlation,
    }
    operation = "create_tunnel"
    idempotency = begin_idempotency(
        credential=credential,
        operation=operation,
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        if not idempotency.record.resource_id:
            raise TunnelUnavailable()
        tunnel = Tunnel.objects.get(pk=idempotency.record.resource_id)
        cycle = tunnel.cycles.get(number=1)
        root = cycle.root_message
        if root is None:
            raise TunnelUnavailable()
        address = encode_token(
            _address_token(
                record=idempotency.record,
                credential=credential,
            )
        )
        response = idempotency.record.response
        tunnel_state = response.get("tunnel_state")
        cycle_state = response.get("cycle_state")
        message_count = response.get("message_count")
        content_bytes = response.get("content_bytes")
        event_position = response.get("event_position")
        if (
            tunnel_state not in Tunnel.State.values
            or cycle_state not in Cycle.State.values
            or not isinstance(message_count, int)
            or isinstance(message_count, bool)
            or message_count < 1
            or not isinstance(content_bytes, int)
            or isinstance(content_bytes, bool)
            or content_bytes < 0
            or not isinstance(event_position, int)
            or isinstance(event_position, bool)
            or event_position < 1
        ):
            raise TunnelUnavailable()
        return CreatedTunnel(
            tunnel=tunnel,
            address=address,
            display_address=display_address(address),
            cycle=cycle,
            root=root,
            tunnel_state=tunnel_state,
            cycle_state=cycle_state,
            message_count=message_count,
            content_bytes=content_bytes,
            event_position=event_position,
            replay=True,
        )
    if new_request_gate:
        new_request_gate()
    _lock_instance_tunnel_quota()
    owned = Tunnel.objects.exclude(state=Tunnel.State.RETIRED)
    maximum = limits.non_retired_tunnels
    if owned.count() >= maximum:
        raise QuotaExceeded()
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits.mentions_per_message,
    )

    now = timezone.now()
    tunnel = Tunnel.objects.create(
        creator=credential,
        label=tunnel_label,
        state=Tunnel.State.ACTIVE,
        next_cycle_number=2,
        next_event_position=3,
    )
    address = _allocate_address(
        tunnel=tunnel,
        credential=credential,
        idempotency_record=idempotency.record,
    )
    cycle = Cycle.objects.create(
        tunnel=tunnel,
        creator=credential,
        number=1,
        label=normalized_cycle_label,
        state=Cycle.State.ACTIVE,
        next_sequence=2,
        expires_at=now + timedelta(seconds=seconds),
        retention_seconds=settings.STARTUNNEL_HISTORY_RETENTION_SECONDS,
    )
    transaction.on_commit(notify_maintenance)
    root = _create_message(
        tunnel=tunnel,
        cycle=cycle,
        sender=credential,
        parent=None,
        sequence=1,
        payload=root_payload,
        correlation_id=correlation,
    )
    _store_mentions(root, mentions)
    cycle.root_message = root
    cycle.message_count = 1
    cycle.content_bytes = root.byte_count
    cycle.save(update_fields=["root_message", "message_count", "content_bytes"])
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=1,
                event_type=TunnelEvent.Type.CYCLE_STARTED,
            ),
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                message=root,
                position=2,
                event_type=TunnelEvent.Type.MESSAGE_POSTED,
            ),
        ]
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    finish_idempotency(
        idempotency.record,
        response={
            "tunnel_id": str(tunnel.id),
            "cycle_id": str(cycle.id),
            "root_message_id": str(root.id),
            "created_at": tunnel.created_at.isoformat(),
            "tunnel_state": Tunnel.State.ACTIVE,
            "cycle_state": Cycle.State.ACTIVE,
            "message_count": 1,
            "content_bytes": root.byte_count,
            "event_position": 2,
        },
        resource_id=str(tunnel.id),
    )
    AuditEvent.objects.create(
        actor_credential=credential,
        action="tunnel.created",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"cycle_id": str(cycle.id)},
    )
    TUNNELS_CREATED.inc()
    return CreatedTunnel(
        tunnel=tunnel,
        address=address,
        display_address=display_address(address),
        cycle=cycle,
        root=root,
        tunnel_state=Tunnel.State.ACTIVE,
        cycle_state=Cycle.State.ACTIVE,
        message_count=1,
        content_bytes=root.byte_count,
        event_position=2,
    )


@transaction.atomic
def post_reply(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    parent_id: UUID,
    content: dict[str, Any],
    mentions: list[UUID] | None = None,
    correlation_id: str | None = None,
) -> PostedReply:
    limits = limits_for(credential)
    payload = validate_content(content, maximum_bytes=limits.bytes_per_message)
    correlation = (correlation_id or "").strip()
    validate_storable_text(correlation, field="correlation_id")
    if len(correlation) > 128:
        raise InvalidRequest("correlation_id can contain at most 128 characters.")
    token = parse_address(address)
    mention_ids = mentions or []
    request_data = {
        "address_digest": address_digest(token).hex(),
        "parent_id": str(parent_id),
        "content": content,
        "mentions": [str(item) for item in mention_ids],
        "correlation_id": correlation,
    }
    idempotency = begin_idempotency(
        credential=credential,
        operation="post_reply",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        if not idempotency.record.resource_id:
            raise TunnelUnavailable()
        message = Message.objects.filter(pk=idempotency.record.resource_id).first()
        if message is None:
            raise TunnelUnavailable()
        event = TunnelEvent.objects.get(message=message, event_type=TunnelEvent.Type.MESSAGE_POSTED)
        return PostedReply(message, event.position, replay=True)

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
    event_position = tunnel.next_event_position
    message = _create_message(
        tunnel=tunnel,
        cycle=cycle,
        sender=credential,
        parent=parent,
        sequence=sequence,
        payload=payload,
        correlation_id=correlation,
    )
    _store_mentions(message, resolved_mentions)
    cycle.next_sequence = sequence + 1
    cycle.message_count += 1
    cycle.content_bytes += message.byte_count
    cycle.save(update_fields=["next_sequence", "message_count", "content_bytes"])
    tunnel.next_event_position = event_position + 1
    tunnel.save(update_fields=["next_event_position"])
    TunnelEvent.objects.create(
        tunnel=tunnel,
        cycle=cycle,
        message=message,
        position=event_position,
        event_type=TunnelEvent.Type.MESSAGE_POSTED,
    )
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
    AuditEvent.objects.create(
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
    limit: int = 1_000,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> TreePage:
    limits = limits_for(credential)
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
        .filter(cycle=cycle, sequence__gt=after_sequence, sequence__lte=high_water)
        .order_by("sequence", "id")[: limit + 1]
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    aggregates = Message.objects.filter(cycle=cycle, sequence__lte=high_water).aggregate(
        count=Count("id"), bytes=Sum("byte_count")
    )
    return TreePage(
        tunnel=tunnel,
        cycle=cycle,
        messages=tuple(page),
        snapshot_sequence=high_water,
        next_after_sequence=page[-1].sequence if has_more and page else None,
        complete=not has_more,
        message_count=aggregates["count"] or 0,
        content_bytes=aggregates["bytes"] or 0,
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
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

    ids: list[UUID] = []
    current: Message | None = focus
    while current is not None:
        ids.append(current.id)
        current = current.parent
    by_id = {
        row.id: row
        for row in Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
        .filter(id__in=ids)
    }
    return tuple(by_id[item] for item in reversed(ids))


def list_replies(
    *,
    credential: AgentCredential,
    address: str,
    message_id: UUID,
    cycle_id: UUID | None = None,
    limit: int = 100,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> ReplyPage:
    parent = get_message(
        credential=credential,
        address=address,
        message_id=message_id,
        cycle_id=cycle_id,
    )
    limits = limits_for(credential)
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
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
        for row in Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
        .filter(id__in=ids)
    }
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
    parameters: list[Any] = [
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
        raise InvalidRequest("The subtree page position is not valid.")
    ids = [row[0] for row in result[1:]]
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

    limits = limits_for(credential)
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
        .filter(cycle=cycle, sequence__lte=high_water)
        .order_by("sequence", "id")
    )
    root = next(row for row in rows if row.id == message_id)
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
    limit: int = 100,
    after_sequence: int = 0,
    snapshot_sequence: int | None = None,
) -> LeafPage:
    """Return a stable sequence-ordered page of messages with no snapshot child."""

    limits = limits_for(credential)
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
        Message.objects.select_related("sender", "parent", "cycle")
        .prefetch_related("mentions__credential")
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


def _can_control_with_credential(*, credential: AgentCredential, tunnel: Tunnel) -> bool:
    return tunnel.creator_id == credential.id


def _current_address(*, tunnel: Tunnel, for_update: bool = False) -> TunnelAddress:
    query = TunnelAddress.objects.filter(tunnel=tunnel, state=TunnelAddress.State.CURRENT)
    if for_update:
        query = query.select_for_update()
    row = query.first()
    if row is None:
        raise TunnelUnavailable()
    return row


def get_tunnel_status(*, credential: AgentCredential, address: str) -> TunnelStatus:
    tunnel = resolve_tunnel(credential=credential, address=address)
    current_address = _current_address(tunnel=tunnel)
    current_cycle = (
        Cycle.objects.select_related("root_message")
        .filter(tunnel=tunnel, state=Cycle.State.ACTIVE)
        .first()
    )
    return TunnelStatus(tunnel=tunnel, current_address=current_address, current_cycle=current_cycle)


def list_cycles(
    *,
    credential: AgentCredential,
    address: str,
    limit: int = 50,
    before_number: int | None = None,
    snapshot_number: int | None = None,
) -> CyclePage:
    limits = limits_for(credential)
    if limit < 1 or limit > limits.maximum_list_page:
        raise InvalidRequest(f"limit must be from 1 through {limits.maximum_list_page}.")
    tunnel = resolve_tunnel(credential=credential, address=address)
    current_high_water = tunnel.next_cycle_number - 1
    high_water = current_high_water if snapshot_number is None else snapshot_number
    if high_water < 1 or high_water > current_high_water:
        raise InvalidRequest("The cycle snapshot is not valid for this tunnel.")
    if before_number is not None and (before_number < 1 or before_number > high_water + 1):
        raise InvalidRequest("The cycle page position is not valid for this tunnel.")
    now = timezone.now()
    query = (
        Cycle.objects.select_related("root_message", "creator")
        .filter(tunnel=tunnel, number__lte=high_water)
        .filter(
            Q(state=Cycle.State.ACTIVE)
            | Q(state=Cycle.State.CLOSED, delete_after__isnull=True)
            | Q(state=Cycle.State.CLOSED, delete_after__gt=now)
        )
        .order_by("-number", "id")
    )
    if before_number is not None:
        query = query.filter(number__lt=before_number)
    rows = [cycle for cycle in query[: limit + 2] if cycle_is_readable(cycle, now=now)]
    has_more = len(rows) > limit
    page = rows[:limit]
    return CyclePage(
        tunnel=tunnel,
        cycles=tuple(page),
        snapshot_number=high_water,
        next_before_number=page[-1].number if has_more and page else None,
        complete=not has_more,
    )


def _prepare_cycle_root(
    *,
    credential: AgentCredential,
    root_content: dict[str, Any],
    cycle_label: str,
    expires_in_seconds: int | None,
    root_mentions: list[UUID] | None,
    root_correlation_id: str | None,
) -> tuple[int, str, ValidatedPayload, str, list[UUID]]:
    limits = limits_for(credential)
    seconds = _cycle_lifetime(expires_in_seconds)
    label = _validate_label(cycle_label, field="A cycle label")
    payload = validate_content(root_content, maximum_bytes=limits.bytes_per_message)
    correlation = (root_correlation_id or "").strip()
    validate_storable_text(correlation, field="correlation_id")
    if len(correlation) > 128:
        raise InvalidRequest("correlation_id can contain at most 128 characters.")
    mention_ids = root_mentions or []
    _validate_mention_ids(mention_ids, maximum=limits.mentions_per_message)
    return seconds, label, payload, correlation, mention_ids


def _start_cycle_locked(
    *,
    tunnel: Tunnel,
    credential: AgentCredential,
    seconds: int,
    label: str,
    payload: ValidatedPayload,
    correlation: str,
    mentions: list[AgentCredential],
    actor_user: User | None = None,
) -> StartedCycle:
    if (
        tunnel.state != Tunnel.State.DORMANT
        or Cycle.objects.filter(tunnel=tunnel, state=Cycle.State.ACTIVE).exists()
    ):
        raise LifecycleConflict()
    now = timezone.now()
    number = tunnel.next_cycle_number
    start_position = tunnel.next_event_position
    message_position = start_position + 1
    cycle = Cycle.objects.create(
        tunnel=tunnel,
        creator=credential,
        number=number,
        label=label,
        state=Cycle.State.ACTIVE,
        next_sequence=2,
        expires_at=now + timedelta(seconds=seconds),
        retention_seconds=settings.STARTUNNEL_HISTORY_RETENTION_SECONDS,
    )
    transaction.on_commit(notify_maintenance)
    root = _create_message(
        tunnel=tunnel,
        cycle=cycle,
        sender=credential,
        parent=None,
        sequence=1,
        payload=payload,
        correlation_id=correlation,
    )
    _store_mentions(root, mentions)
    cycle.root_message = root
    cycle.message_count = 1
    cycle.content_bytes = root.byte_count
    cycle.save(update_fields=["root_message", "message_count", "content_bytes"])
    tunnel.state = Tunnel.State.ACTIVE
    tunnel.dormant_at = None
    tunnel.next_cycle_number = number + 1
    tunnel.next_event_position = message_position + 1
    tunnel.save(update_fields=["state", "dormant_at", "next_cycle_number", "next_event_position"])
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=start_position,
                event_type=TunnelEvent.Type.CYCLE_STARTED,
            ),
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                message=root,
                position=message_position,
                event_type=TunnelEvent.Type.MESSAGE_POSTED,
            ),
        ]
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    AuditEvent.objects.create(
        actor_credential=credential if actor_user is None else None,
        actor_user=actor_user,
        action="cycle.started",
        target_type="cycle",
        target_id=cycle.id,
        request_id=current_request_id(),
        metadata={"number": number},
    )
    return StartedCycle(
        tunnel=tunnel,
        cycle=cycle,
        root=root,
        cycle_state=Cycle.State.ACTIVE,
        message_count=1,
        content_bytes=root.byte_count,
        event_position=message_position,
    )


def _replayed_cycle(record: IdempotencyRecord) -> StartedCycle:
    if record.resource_id is None:
        raise TunnelUnavailable()
    event_position = record.response.get("event_position")
    cycle_state = record.response.get("cycle_state")
    message_count = record.response.get("message_count")
    content_bytes = record.response.get("content_bytes")
    if (
        not isinstance(event_position, int)
        or isinstance(event_position, bool)
        or event_position < 1
        or cycle_state not in Cycle.State.values
        or not isinstance(message_count, int)
        or isinstance(message_count, bool)
        or message_count < 1
        or not isinstance(content_bytes, int)
        or isinstance(content_bytes, bool)
        or content_bytes < 0
    ):
        raise TunnelUnavailable()
    cycle = Cycle.objects.select_related("root_message", "tunnel").get(pk=record.resource_id)
    if cycle.root_message is None:
        raise TunnelUnavailable()
    return StartedCycle(
        tunnel=cycle.tunnel,
        cycle=cycle,
        root=cycle.root_message,
        cycle_state=cycle_state,
        message_count=message_count,
        content_bytes=content_bytes,
        event_position=event_position,
        replay=True,
    )


def _cycle_request_data(
    *,
    address: str,
    expected_address_generation: int,
    expected_cycle_id: UUID | None,
    cycle_label: str,
    expires_in_seconds: int,
    root_content: dict[str, Any],
    root_mentions: list[UUID],
    root_correlation_id: str,
) -> dict[str, Any]:
    return {
        "address_digest": address_digest(parse_address(address)).hex(),
        "expected_address_generation": expected_address_generation,
        "expected_cycle_id": str(expected_cycle_id) if expected_cycle_id else None,
        "cycle_label": cycle_label,
        "expires_in_seconds": expires_in_seconds,
        "root_content": root_content,
        "root_mentions": [str(value) for value in root_mentions],
        "root_correlation_id": root_correlation_id,
    }


@transaction.atomic
def start_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
) -> StartedCycle:
    if not credential_is_active(credential):
        raise InvalidCredential()
    seconds, label, payload, correlation, mention_ids = _prepare_cycle_root(
        credential=credential,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=root_mentions,
        root_correlation_id=root_correlation_id,
    )
    request_data = _cycle_request_data(
        address=address,
        expected_address_generation=expected_address_generation,
        expected_cycle_id=None,
        cycle_label=label,
        expires_in_seconds=seconds,
        root_content=root_content,
        root_mentions=root_mentions or [],
        root_correlation_id=correlation,
    )
    operation = "start_cycle"
    replay = find_idempotency_replay(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if replay:
        return _replayed_cycle(replay.record)
    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits_for(credential).mentions_per_message,
    )
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    idempotency = begin_idempotency(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if idempotency.replay:
        return _replayed_cycle(idempotency.record)
    started = _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=mentions,
    )
    finish_idempotency(
        idempotency.record,
        response={
            "cycle_id": str(started.cycle.id),
            "cycle_state": started.cycle_state,
            "message_count": started.message_count,
            "content_bytes": started.content_bytes,
            "event_position": started.event_position,
            "started": True,
        },
        resource_id=str(started.cycle.id),
    )
    return started


def _close_for_rollover_locked(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    actor_credential: AgentCredential,
    actor_user: User | None = None,
) -> None:
    if cycle.state != Cycle.State.ACTIVE or tunnel.state != Tunnel.State.ACTIVE:
        raise LifecycleConflict()
    _close_locked_cycle(
        tunnel=tunnel,
        cycle=cycle,
        reason="rollover",
        actor_credential=actor_credential if actor_user is None else None,
        actor_user=actor_user,
    )


@transaction.atomic
def rollover_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_cycle_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
    root_mentions: list[UUID] | None = None,
    root_correlation_id: str | None = None,
) -> StartedCycle:
    if not credential_is_active(credential):
        raise InvalidCredential()
    seconds, label, payload, correlation, mention_ids = _prepare_cycle_root(
        credential=credential,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=root_mentions,
        root_correlation_id=root_correlation_id,
    )
    request_data = _cycle_request_data(
        address=address,
        expected_address_generation=expected_address_generation,
        expected_cycle_id=expected_cycle_id,
        cycle_label=label,
        expires_in_seconds=seconds,
        root_content=root_content,
        root_mentions=root_mentions or [],
        root_correlation_id=correlation,
    )
    operation = "rollover_cycle"
    replay = find_idempotency_replay(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if replay:
        return _replayed_cycle(replay.record)
    tunnel = resolve_tunnel(credential=credential, address=address, for_update=True)
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential, operation=operation, key=idempotency_key, request_data=request_data
    )
    if idempotency.replay:
        return _replayed_cycle(idempotency.record)
    mentions = _resolve_mentions(
        mention_ids=mention_ids,
        maximum=limits_for(credential).mentions_per_message,
    )
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    cycle = active_cycle(tunnel=tunnel, for_update=True)
    if cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_for_rollover_locked(
        tunnel=tunnel,
        cycle=cycle,
        actor_credential=credential,
    )
    started = _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=mentions,
    )
    finish_idempotency(
        idempotency.record,
        response={
            "closed_cycle_id": str(cycle.id),
            "cycle_id": str(started.cycle.id),
            "cycle_state": started.cycle_state,
            "message_count": started.message_count,
            "content_bytes": started.content_bytes,
            "event_position": started.event_position,
            "started": True,
        },
        resource_id=str(started.cycle.id),
    )
    return started


def _close_locked_cycle(
    *,
    tunnel: Tunnel,
    cycle: Cycle,
    reason: str,
    actor_credential: AgentCredential | None = None,
    actor_user: User | None = None,
) -> None:
    if cycle.state != Cycle.State.ACTIVE or tunnel.state != Tunnel.State.ACTIVE:
        return
    now = timezone.now()
    closed_at = cycle.expires_at if reason == "expired" else now
    retention_seconds = (
        cycle.retention_seconds
        if reason == "expired"
        else settings.STARTUNNEL_HISTORY_RETENTION_SECONDS
    )
    close_position = tunnel.next_event_position
    dormant_position = close_position + 1
    cycle.state = Cycle.State.CLOSED
    cycle.closed_at = closed_at
    cycle.retention_seconds = retention_seconds
    cycle.delete_after = (
        closed_at + timedelta(seconds=retention_seconds) if retention_seconds is not None else None
    )
    cycle.close_reason = reason
    cycle.final_sequence = cycle.next_sequence - 1
    cycle.save(
        update_fields=[
            "state",
            "closed_at",
            "retention_seconds",
            "delete_after",
            "close_reason",
            "final_sequence",
        ]
    )
    if reason != "expired":
        transaction.on_commit(notify_maintenance)
    tunnel.state = Tunnel.State.DORMANT
    tunnel.dormant_at = now
    tunnel.next_event_position = dormant_position + 1
    tunnel.save(update_fields=["state", "dormant_at", "next_event_position"])
    TunnelEvent.objects.bulk_create(
        [
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=close_position,
                event_type=TunnelEvent.Type.CYCLE_CLOSED,
                metadata={"reason": reason},
            ),
            TunnelEvent(
                tunnel=tunnel,
                cycle=cycle,
                position=dormant_position,
                event_type=TunnelEvent.Type.TUNNEL_DORMANT,
            ),
        ]
    )
    notify(ACTIVITY_CHANNEL, str(tunnel.id))
    AuditEvent.objects.create(
        actor_credential=actor_credential,
        actor_user=actor_user,
        action="cycle.closed",
        target_type="cycle",
        target_id=cycle.id,
        request_id=current_request_id(),
        metadata={"reason": reason},
    )


@transaction.atomic
def close_cycle(
    *,
    credential: AgentCredential,
    idempotency_key: str,
    address: str,
    expected_cycle_id: UUID,
) -> None:
    token = parse_address(address)
    request_data = {
        "address_digest": address_digest(token).hex(),
        "expected_cycle_id": str(expected_cycle_id),
    }
    replay = find_idempotency_replay(
        credential=credential,
        operation="close_cycle",
        key=idempotency_key,
        request_data=request_data,
    )
    if replay:
        return
    tunnel = resolve_tunnel(
        credential=credential,
        address=address,
        for_update=True,
    )
    if not _can_control_with_credential(credential=credential, tunnel=tunnel):
        raise TunnelUnavailable()
    idempotency = begin_idempotency(
        credential=credential,
        operation="close_cycle",
        key=idempotency_key,
        request_data=request_data,
    )
    if idempotency.replay:
        return
    cycle = active_cycle(tunnel=tunnel, for_update=True)
    if cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_locked_cycle(tunnel=tunnel, cycle=cycle, reason="creator", actor_credential=credential)
    finish_idempotency(
        idempotency.record,
        response={"cycle_id": str(cycle.id), "closed": True},
        resource_id=str(cycle.id),
    )


def get_tunnel(*, credential: AgentCredential, tunnel_id: UUID) -> Tunnel:
    tunnel = Tunnel.objects.filter(pk=tunnel_id, creator=credential).first()
    if not tunnel:
        raise TunnelUnavailable()
    return tunnel


@transaction.atomic
def close_cycle_as_operator(*, actor: User, tunnel_id: UUID, expected_cycle_id: UUID) -> None:
    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise TunnelUnavailable()
    tunnel = (
        Tunnel.objects.select_for_update(of=("self",))
        .select_related("creator")
        .filter(pk=tunnel_id)
        .first()
    )
    if not tunnel:
        raise TunnelUnavailable()
    cycle = (
        Cycle.objects.select_for_update().filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    )
    if cycle is None or cycle.id != expected_cycle_id:
        raise LifecycleConflict()
    _close_locked_cycle(tunnel=tunnel, cycle=cycle, reason="instance_operator", actor_user=actor)


def _locked_operator_tunnel(*, actor: User, tunnel_id: UUID) -> Tunnel:
    if not User.objects.filter(pk=actor.pk, is_active=True).exists():
        raise TunnelUnavailable()
    tunnel = (
        Tunnel.objects.select_for_update(of=("self",))
        .select_related("creator")
        .filter(pk=tunnel_id)
        .first()
    )
    if tunnel is None:
        raise TunnelUnavailable()
    return tunnel


def _start_cycle_as_human_locked(
    *,
    tunnel: Tunnel,
    actor: User,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str,
    expires_in_seconds: int | None,
    expected_cycle_id: UUID | None,
) -> StartedCycle:
    credential = tunnel.creator
    if not credential_is_active(credential):
        raise TunnelUnavailable()
    current_address = _current_address(tunnel=tunnel, for_update=True)
    if current_address.generation != expected_address_generation:
        raise LifecycleConflict()
    seconds, label, payload, correlation, mention_ids = _prepare_cycle_root(
        credential=credential,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        root_mentions=[],
        root_correlation_id=None,
    )
    if expected_cycle_id is not None:
        cycle = active_cycle(tunnel=tunnel, for_update=True)
        if cycle.id != expected_cycle_id:
            raise LifecycleConflict()
        _close_for_rollover_locked(
            tunnel=tunnel,
            cycle=cycle,
            actor_credential=credential,
            actor_user=actor,
        )
    return _start_cycle_locked(
        tunnel=tunnel,
        credential=credential,
        seconds=seconds,
        label=label,
        payload=payload,
        correlation=correlation,
        mentions=_resolve_mentions(
            mention_ids=mention_ids,
            maximum=limits_for(credential).mentions_per_message,
        ),
        actor_user=actor,
    )


@transaction.atomic
def start_cycle_as_operator(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
) -> StartedCycle:
    tunnel = _locked_operator_tunnel(actor=actor, tunnel_id=tunnel_id)
    return _start_cycle_as_human_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        expected_cycle_id=None,
    )


@transaction.atomic
def rollover_cycle_as_operator(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_cycle_id: UUID,
    expected_address_generation: int,
    root_content: dict[str, Any],
    cycle_label: str = "",
    expires_in_seconds: int | None = None,
) -> StartedCycle:
    tunnel = _locked_operator_tunnel(actor=actor, tunnel_id=tunnel_id)
    return _start_cycle_as_human_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        root_content=root_content,
        cycle_label=cycle_label,
        expires_in_seconds=expires_in_seconds,
        expected_cycle_id=expected_cycle_id,
    )


def retirement_confirmation(tunnel: Tunnel) -> str:
    return tunnel.label or str(tunnel.id)


def _rotate_locked_address(
    *, tunnel: Tunnel, actor: User, expected_address_generation: int
) -> RotatedAddress:
    if tunnel.state == Tunnel.State.RETIRED:
        raise LifecycleConflict()
    current = _current_address(tunnel=tunnel, for_update=True)
    if current.generation != expected_address_generation:
        raise LifecycleConflict()
    now = timezone.now()
    current.state = TunnelAddress.State.RETIRED
    current.retired_at = now
    current.retirement_reason = "operator_rotation"
    current.save(update_fields=["state", "retired_at", "retirement_reason"])
    generation = current.generation + 1
    _row, address = _allocate_random_address(tunnel=tunnel, generation=generation)
    event_position = tunnel.next_event_position
    tunnel.next_event_position = event_position + 1
    tunnel.save(update_fields=["next_event_position"])
    TunnelEvent.objects.create(
        tunnel=tunnel,
        position=event_position,
        event_type=TunnelEvent.Type.ADDRESS_ROTATED,
        metadata={"generation": generation},
    )
    AuditEvent.objects.create(
        actor_user=actor,
        action="tunnel.address_rotated",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"generation": generation},
    )
    return RotatedAddress(
        tunnel=tunnel,
        address=address,
        display_address=display_address(address),
        generation=generation,
    )


@transaction.atomic
def rotate_address_as_operator(
    *, actor: User, tunnel_id: UUID, expected_address_generation: int
) -> RotatedAddress:
    tunnel = _locked_operator_tunnel(actor=actor, tunnel_id=tunnel_id)
    return _rotate_locked_address(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
    )


def _retire_locked_tunnel(
    *,
    tunnel: Tunnel,
    reason: str,
    actor_user: User | None = None,
) -> None:
    """Retire one locked tunnel and keep its immutable cycle content."""

    cycle = (
        Cycle.objects.select_for_update().filter(tunnel=tunnel, state=Cycle.State.ACTIVE).first()
    )
    if cycle:
        _close_locked_cycle(
            tunnel=tunnel,
            cycle=cycle,
            reason=reason,
            actor_user=actor_user,
        )
        tunnel.refresh_from_db()
    now = timezone.now()
    TunnelAddress.objects.select_for_update().filter(
        tunnel=tunnel,
        state=TunnelAddress.State.CURRENT,
    ).update(
        state=TunnelAddress.State.RETIRED,
        retired_at=now,
        retirement_reason=reason,
    )
    event_position = tunnel.next_event_position
    tunnel.state = Tunnel.State.RETIRED
    tunnel.retired_at = now
    tunnel.next_event_position = event_position + 1
    tunnel.save(update_fields=["state", "retired_at", "next_event_position"])
    TunnelEvent.objects.create(
        tunnel=tunnel,
        position=event_position,
        event_type=TunnelEvent.Type.TUNNEL_RETIRED,
        metadata={"reason": reason},
    )
    AuditEvent.objects.create(
        actor_user=actor_user,
        action="tunnel.retired",
        target_type="tunnel",
        target_id=tunnel.id,
        request_id=current_request_id(),
        metadata={"reason": reason},
    )


def _retire_as_human_locked(
    *,
    tunnel: Tunnel,
    actor: User,
    expected_address_generation: int,
    confirmation: str,
) -> None:
    if confirmation != retirement_confirmation(tunnel):
        raise InvalidRequest("The retirement confirmation does not match.")
    current = _current_address(tunnel=tunnel, for_update=True)
    if current.generation != expected_address_generation:
        raise LifecycleConflict()
    _retire_locked_tunnel(tunnel=tunnel, reason="operator_retirement", actor_user=actor)


@transaction.atomic
def retire_tunnel_as_operator(
    *,
    actor: User,
    tunnel_id: UUID,
    expected_address_generation: int,
    confirmation: str,
) -> None:
    tunnel = _locked_operator_tunnel(actor=actor, tunnel_id=tunnel_id)
    _retire_as_human_locked(
        tunnel=tunnel,
        actor=actor,
        expected_address_generation=expected_address_generation,
        confirmation=confirmation,
    )


@transaction.atomic
def retire_tunnels_for_credentials(*, credential_ids: list[UUID], reason: str) -> int:
    """Retire tunnels from replaced fixture credentials and keep their cycle content."""

    tunnels = list(
        Tunnel.objects.select_for_update()
        .filter(creator_id__in=credential_ids)
        .exclude(state=Tunnel.State.RETIRED)
        .order_by("id")
    )
    for tunnel in tunnels:
        _retire_locked_tunnel(tunnel=tunnel, reason=reason)
    return len(tunnels)
