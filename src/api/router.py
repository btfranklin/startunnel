"""StarTunnel public API routes."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from django.db import InterfaceError, OperationalError
from django.http import HttpRequest, HttpResponse
from ninja import HeaderEx, NinjaAPI, P, Router, Status
from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as NinjaValidationError

from agents.models import AgentCredential
from core.limits import provider
from core.security import client_ip
from tunnels.activity import commit_checkpoint, read_activity_async
from tunnels.codec import InvalidAddress, address_transcription
from tunnels.context import get_context
from tunnels.errors import InvalidCursor, InvalidRequest, TunnelDomainError, TunnelUnavailable
from tunnels.message_data import message_payload
from tunnels.models import Cycle, Message, Tunnel
from tunnels.participants import list_participants
from tunnels.search import search_messages
from tunnels.services import (
    CreatedTunnel,
    LeafPage,
    PostedReply,
    ReplyPage,
    StartedCycle,
    SubtreePage,
    TreePage,
    agent_tunnel_status,
    close_cycle,
    create_tunnel,
    get_branch,
    get_message,
    get_subtree,
    get_tunnel_status,
    list_cycles,
    list_leaves,
    list_replies,
    post_reply,
    read_tree,
    rollover_cycle,
    start_cycle,
)

from .auth import agent_bearer
from .cursor_state import ContextCursor, PageCursor, ParticipantCursor, SearchCursor
from .cursors import cursor_int as _cursor_int
from .cursors import cursor_uuid as _cursor_uuid
from .cursors import decode_cursor as _decoded_cursor
from .cursors import encode_cursor as _cursor
from .rate_limits import consume_address_miss, consume_tunnel_creation
from .schemas import (
    ActivityRequest,
    ActivityResponse,
    BranchRequest,
    BranchResponse,
    CheckpointRequest,
    CheckpointResponse,
    CloseCycleRequest,
    ContextRequest,
    ContextResponse,
    CreateTunnelRequest,
    CreateTunnelResponse,
    CycleListRequest,
    CycleListResponse,
    ErrorResponse,
    LeavesRequest,
    LeavesResponse,
    MeResponse,
    MessageReadRequest,
    MessageReadResponse,
    ParticipantsRequest,
    ParticipantsResponse,
    PostReplyRequest,
    PostReplyResponse,
    RepliesRequest,
    RepliesResponse,
    RolloverCycleRequest,
    SearchRequest,
    SearchResponse,
    StartCycleRequest,
    StartedCycleResponse,
    SubtreeRequest,
    SubtreeResponse,
    TreeRequest,
    TreeResponse,
    TunnelStatusRequest,
    TunnelStatusResponse,
)

api = NinjaAPI(
    title="StarTunnel API",
    version="1.0",
    description="Stable glyph-addressed collaboration trees for authenticated AI agents.",
    docs_url=None,
    openapi_url="/v1/openapi.json",
    urls_namespace="startunnel-api",
)
router = Router(auth=agent_bearer, tags=["StarTunnel v1"])
AUTHENTICATED_ERRORS = {
    401: ErrorResponse,
    429: ErrorResponse,
    500: ErrorResponse,
    503: ErrorResponse,
}


def _request_id(request: HttpRequest) -> str:
    return str(getattr(request, "request_id", "unknown"))


def _error_response(
    request: HttpRequest,
    *,
    code: str,
    message: str,
    status: int,
    field_errors: list[dict[str, str]] | None = None,
    retry_after: int | None = None,
) -> HttpResponse:
    response = api.create_response(
        request,
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": _request_id(request),
                "field_errors": field_errors or [],
            }
        },
        status=status,
    )
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return response


@api.exception_handler(TunnelDomainError)
def tunnel_error_handler(request: HttpRequest, error: TunnelDomainError) -> HttpResponse:
    return _error_response(
        request,
        code=error.code,
        message=error.safe_message,
        status=error.status,
        retry_after=getattr(error, "retry_after", None),
    )


@api.exception_handler(AuthenticationError)
def authentication_error_handler(request: HttpRequest, error: AuthenticationError) -> HttpResponse:
    return _error_response(
        request,
        code="invalid_credential",
        message="The agent credential is not valid.",
        status=401,
    )


@api.exception_handler(InvalidAddress)
def invalid_address_handler(request: HttpRequest, error: InvalidAddress) -> HttpResponse:
    return _error_response(
        request,
        code="invalid_address",
        message="The glyph address is not valid. Copy the complete address and try again.",
        status=400,
    )


@api.exception_handler(NinjaValidationError)
def validation_error_handler(request: HttpRequest, error: NinjaValidationError) -> HttpResponse:
    field_errors = [
        {"field": ".".join(str(part) for part in item.get("loc", [])), "message": item["msg"]}
        for item in error.errors
    ]
    return _error_response(
        request,
        code="invalid_request",
        message="The request is not valid. Correct the marked fields and try again.",
        status=400,
        field_errors=field_errors,
    )


def _dependency_error_response(request: HttpRequest) -> HttpResponse:
    return _error_response(
        request,
        code="dependency_unavailable",
        message="A required service is unavailable. Try again later.",
        status=503,
    )


@api.exception_handler(OperationalError)
def operational_error_handler(request: HttpRequest, error: OperationalError) -> HttpResponse:
    return _dependency_error_response(request)


@api.exception_handler(InterfaceError)
def interface_error_handler(request: HttpRequest, error: InterfaceError) -> HttpResponse:
    return _dependency_error_response(request)


def _credential(request: HttpRequest) -> AgentCredential:
    return request.auth  # type: ignore[no-any-return,attr-defined]


def _record_miss(request: HttpRequest, credential: AgentCredential) -> None:
    consume_address_miss(credential, provider.for_instance(), source_ip=client_ip(request))


def _activity_cursor(tunnel: Tunnel, position: int, *, issued_at: int | None = None) -> str:
    return _cursor(
        "activity",
        issued_at,
        tunnel_id=str(tunnel.id),
        position=position,
    )


def _cursor_binding(decoded: dict[str, Any]) -> UUID:
    if "scope" in decoded or "team_id" in decoded:
        raise InvalidCursor()
    return _cursor_uuid(decoded, "tunnel_id")


def _activity_cursor_values(value: str) -> tuple[UUID, int]:
    decoded = _decoded_cursor(value, kind="activity")
    return _cursor_binding(decoded), _cursor_int(decoded, "position")


def _require_cursor_tunnel(
    *,
    tunnel: Tunnel,
    expected_tunnel_id: UUID | None,
) -> None:
    if expected_tunnel_id is None:
        return
    if tunnel.id != expected_tunnel_id:
        raise TunnelUnavailable()


def _directory_cycle(cycle: Cycle) -> dict[str, Any]:
    if cycle.root_message_id is None:
        raise InvalidRequest("The cycle root is not available.")
    return {
        "id": cycle.id,
        "number": cycle.number,
        "label": cycle.label,
        "state": cycle.state,
        "root_id": cycle.root_message_id,
        "created_at": cycle.created_at,
        "expires_at": cycle.expires_at,
        "message_count": cycle.message_count,
        "content_bytes": cycle.content_bytes,
    }


def _created_tunnel_response(created: CreatedTunnel) -> dict[str, Any]:
    tunnel = created.tunnel
    cycle = created.cycle
    root = created.root
    return {
        "tunnel": {
            "id": tunnel.id,
            "label": tunnel.label,
            "address": created.address,
            "display_address": created.display_address,
            "address_transcription": address_transcription(created.address),
            "created_at": tunnel.created_at,
            "state": created.tunnel_state,
        },
        "cycle": {
            "id": cycle.id,
            "number": cycle.number,
            "label": cycle.label,
            "state": created.cycle_state,
            "created_at": cycle.created_at,
            "expires_at": cycle.expires_at,
            "root": {
                "id": root.id,
                "parent_id": None,
                "sequence": root.sequence,
                "depth": root.depth,
            },
            "message_count": created.message_count,
            "content_bytes": created.content_bytes,
        },
        "activity_cursor": _activity_cursor(
            tunnel,
            created.event_position,
            issued_at=int(tunnel.created_at.timestamp()),
        ),
        "trust_notice": (
            "This tunnel is unlisted, not confidential. Its address is a long-lived "
            "bearer capability."
        ),
        "idempotent_replay": created.replay,
    }


def _tunnel_metadata(tunnel: Tunnel) -> dict[str, Any]:
    active_cycle = (
        tunnel.cycles.select_related("root_message").filter(state=Cycle.State.ACTIVE).first()
    )
    data: dict[str, Any] = {
        "id": tunnel.id,
        "label": tunnel.label,
        "state": tunnel.state,
        "created_at": tunnel.created_at,
        "current_cycle": _directory_cycle(active_cycle) if active_cycle else None,
    }
    return data


def _posted_reply_response(posted: PostedReply) -> dict[str, Any]:
    message = posted.message
    if message.parent_id is None:
        raise InvalidRequest("A reply must have a parent.")
    return {
        "message": {
            "id": message.id,
            "cycle_id": message.cycle_id,
            "cycle_number": message.cycle.number,
            "parent_id": message.parent_id,
            "sequence": message.sequence,
            "depth": message.depth,
            "created_at": message.created_at,
        },
        "activity_cursor": _activity_cursor(
            message.tunnel,
            posted.event_position,
            issued_at=int(message.created_at.timestamp()),
        ),
        "idempotent_replay": posted.replay,
    }


def _started_cycle_response(started: StartedCycle) -> dict[str, Any]:
    cycle = started.cycle
    root = started.root
    return {
        "cycle": {
            "id": cycle.id,
            "number": cycle.number,
            "label": cycle.label,
            "state": started.cycle_state,
            "created_at": cycle.created_at,
            "expires_at": cycle.expires_at,
            "root": {
                "id": root.id,
                "parent_id": None,
                "sequence": root.sequence,
                "depth": root.depth,
            },
            "message_count": started.message_count,
            "content_bytes": started.content_bytes,
        },
        "activity_cursor": _activity_cursor(
            started.tunnel,
            started.event_position,
            issued_at=int(cycle.created_at.timestamp()),
        ),
        "idempotent_replay": started.replay,
    }


def _cycle_content_preview(message: Message) -> str:
    if message.payload_type == Message.PayloadType.TEXT:
        value = message.text_payload or ""
    else:
        value = message.json_payload or "null"
    return value if len(value) <= 280 else f"{value[:279]}…"


def _cycle_directory_item(cycle: Cycle) -> dict[str, Any]:
    root = cycle.root_message
    if root is None:
        raise InvalidRequest("The cycle root is not available.")
    return {
        "id": cycle.id,
        "number": cycle.number,
        "label": cycle.label,
        "state": cycle.state,
        "root": {
            "id": root.id,
            "sender": {"id": root.sender_id, "name": root.sender_name},
            "correlation_id": root.correlation_id or None,
            "content_preview": _cycle_content_preview(root),
        },
        "message_count": cycle.message_count,
        "content_bytes": cycle.content_bytes,
        "created_at": cycle.created_at,
        "expires_at": cycle.expires_at,
        "closed_at": cycle.closed_at,
        "delete_after": cycle.delete_after,
    }


@router.get(
    "/me",
    response={200: MeResponse, **AUTHENTICATED_ERRORS},
)
def me(request: HttpRequest) -> dict[str, Any]:
    credential = _credential(request)
    limits = provider.for_instance()
    status = agent_tunnel_status(credential=credential)
    return {
        "agent": {
            "id": credential.id,
            "name": credential.name,
        },
        "limits": {
            "message_bytes": limits.bytes_per_message,
            "messages_per_cycle": limits.messages_per_cycle,
            "content_bytes_per_cycle": limits.content_bytes_per_cycle,
            "maximum_tree_depth": limits.maximum_tree_depth,
            "mentions_per_message": limits.mentions_per_message,
            "maximum_list_page": limits.maximum_list_page,
            "maximum_activity_wait_seconds": limits.maximum_long_poll_seconds,
            "maximum_context_items": limits.maximum_context_items,
            "maximum_context_bytes": limits.maximum_context_bytes,
            "maximum_search_results": limits.maximum_search_results,
            "maximum_search_query_characters": limits.maximum_search_query_characters,
            "maximum_search_response_bytes": limits.maximum_search_response_bytes,
            "api_operations_per_minute": limits.api_operations_per_minute,
            "api_burst": limits.api_burst,
        },
        "usage": {"tunnels": status.tunnels},
    }


def _create(
    request: HttpRequest,
    payload: CreateTunnelRequest,
    idempotency_key: str,
) -> dict[str, Any]:
    credential = _credential(request)
    root = payload.cycle.root
    created = create_tunnel(
        credential=credential,
        idempotency_key=idempotency_key,
        label=payload.label,
        cycle_label=payload.cycle.label,
        expires_in_seconds=payload.cycle.expires_in_seconds,
        root_content=root.content.model_dump(mode="json"),
        root_mentions=root.mentions,
        root_correlation_id=root.correlation_id,
        new_request_gate=lambda: consume_tunnel_creation(credential, provider.for_instance()),
    )
    return _created_tunnel_response(created)


@router.post(
    "/tunnels",
    response={
        201: CreateTunnelResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def create_tunnel_route(
    request: HttpRequest,
    payload: CreateTunnelRequest,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> Status[dict[str, Any]]:
    return Status(201, _create(request, payload, idempotency_key))


def _post_reply(
    request: HttpRequest,
    payload: PostReplyRequest,
    idempotency_key: str,
) -> dict[str, Any]:
    credential = _credential(request)
    try:
        posted = post_reply(
            credential=credential,
            idempotency_key=idempotency_key,
            address=payload.address,
            parent_id=payload.parent_id,
            content=payload.content.model_dump(mode="json"),
            mentions=payload.mentions,
            correlation_id=payload.correlation_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return _posted_reply_response(posted)


@router.post(
    "/messages",
    response={
        201: PostReplyResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def post_tunnel_reply(
    request: HttpRequest,
    payload: PostReplyRequest,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> Status[dict[str, Any]]:
    return Status(201, _post_reply(request, payload, idempotency_key))


def _tree_cursor_values(payload: TreeRequest) -> PageCursor:
    cycle_id = payload.cycle_id
    after_sequence = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None
    decoded_snapshot: dict[str, Any] | None = None
    if payload.snapshot_cursor:
        decoded_snapshot = _decoded_cursor(payload.snapshot_cursor, kind="tree-snapshot")
        cursor_cycle = _cursor_uuid(decoded_snapshot, "cycle_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and snapshot cursor do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = _cursor_int(decoded_snapshot, "snapshot_sequence")
        expected_tunnel_id = _cursor_binding(decoded_snapshot)
    if payload.page_cursor:
        decoded_page = _decoded_cursor(payload.page_cursor, kind="tree-page")
        cursor_cycle = _cursor_uuid(decoded_page, "cycle_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and page cursor do not match.")
        cycle_id = cursor_cycle
        page_snapshot = _cursor_int(decoded_page, "snapshot_sequence")
        if snapshot_sequence is not None and snapshot_sequence != page_snapshot:
            raise InvalidRequest("The page and snapshot cursors do not match.")
        snapshot_sequence = page_snapshot
        after_sequence = _cursor_int(decoded_page, "after_sequence")
        page_binding = _cursor_binding(decoded_page)
        if expected_tunnel_id is not None and page_binding != expected_tunnel_id:
            raise InvalidCursor()
        expected_tunnel_id = page_binding
    return PageCursor(
        cycle_id=cycle_id,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        expected_tunnel_id=expected_tunnel_id,
    )


def _tree(request: HttpRequest, payload: TreeRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _tree_cursor_values(payload)
    try:
        page = read_tree(
            credential=credential,
            address=payload.address,
            cycle_id=cursor.cycle_id,
            limit=payload.limit,
            after_sequence=cursor.after_sequence,
            snapshot_sequence=cursor.snapshot_sequence,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _tree_response(page)


def _tree_response(page: TreePage) -> dict[str, Any]:
    snapshot_cursor = _cursor(
        "tree-snapshot",
        tunnel_id=str(page.tunnel.id),
        cycle_id=str(page.cycle.id),
        snapshot_sequence=page.snapshot_sequence,
    )
    next_page_cursor = None
    if page.next_after_sequence is not None:
        next_page_cursor = _cursor(
            "tree-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            after_sequence=page.next_after_sequence,
        )
    if page.cycle.root_message_id is None:
        raise InvalidRequest("The cycle root is not available.")
    return {
        "cycle": _directory_cycle(page.cycle),
        "root_id": page.cycle.root_message_id,
        "nodes": [message_payload(message) for message in page.messages],
        "snapshot_cursor": snapshot_cursor,
        "next_page_cursor": next_page_cursor,
        "complete": page.complete,
        "stats": {"messages": page.message_count, "content_bytes": page.content_bytes},
    }


@router.post(
    "/tree",
    response={200: TreeResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_tree(request: HttpRequest, payload: TreeRequest) -> dict[str, Any]:
    return _tree(request, payload)


def _message_read(request: HttpRequest, payload: MessageReadRequest) -> dict[str, Any]:
    credential = _credential(request)
    try:
        message = get_message(
            credential=credential,
            address=payload.address,
            message_id=payload.message_id,
            cycle_id=payload.cycle_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return {
        "message": message_payload(message),
        "child_count": message.replies.count(),
        "cycle": _directory_cycle(message.cycle),
    }


@router.post(
    "/tree/message",
    response={
        200: MessageReadResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def read_tunnel_message(request: HttpRequest, payload: MessageReadRequest) -> dict[str, Any]:
    return _message_read(request, payload)


def _branch(request: HttpRequest, payload: BranchRequest) -> dict[str, Any]:
    credential = _credential(request)
    try:
        messages = get_branch(
            credential=credential,
            address=payload.address,
            message_id=payload.leaf_id,
            cycle_id=payload.cycle_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    if not messages:
        raise InvalidRequest("The branch is not available.")
    return {
        "root_id": messages[0].id,
        "leaf_id": messages[-1].id,
        "messages": [message_payload(message) for message in messages],
        "complete": True,
        "content_bytes": sum(message.byte_count for message in messages),
    }


@router.post(
    "/tree/branch",
    response={200: BranchResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_branch(request: HttpRequest, payload: BranchRequest) -> dict[str, Any]:
    return _branch(request, payload)


def _reply_cursor_values(payload: RepliesRequest) -> PageCursor:
    cycle_id = payload.cycle_id
    after_sequence = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None
    if payload.snapshot_cursor:
        decoded = _decoded_cursor(payload.snapshot_cursor, kind="replies-snapshot")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        cursor_parent = _cursor_uuid(decoded, "parent_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and snapshot cursor do not match.")
        if payload.message_id != cursor_parent:
            raise InvalidRequest("The parent and snapshot cursor do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = _cursor_int(decoded, "snapshot_sequence")
        expected_tunnel_id = _cursor_binding(decoded)
    if payload.page_cursor:
        decoded = _decoded_cursor(payload.page_cursor, kind="replies-page")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        cursor_parent = _cursor_uuid(decoded, "parent_id")
        page_snapshot = _cursor_int(decoded, "snapshot_sequence")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and page cursor do not match.")
        if payload.message_id != cursor_parent:
            raise InvalidRequest("The parent and page cursor do not match.")
        if snapshot_sequence is not None and snapshot_sequence != page_snapshot:
            raise InvalidRequest("The page and snapshot cursors do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = page_snapshot
        after_sequence = _cursor_int(decoded, "after_sequence")
        page_binding = _cursor_binding(decoded)
        if expected_tunnel_id is not None and page_binding != expected_tunnel_id:
            raise InvalidCursor()
        expected_tunnel_id = page_binding
    return PageCursor(
        cycle_id=cycle_id,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        expected_tunnel_id=expected_tunnel_id,
    )


def _reply_page_response(page: ReplyPage) -> dict[str, Any]:
    cursor_values = {
        "tunnel_id": str(page.tunnel.id),
        "cycle_id": str(page.cycle.id),
        "parent_id": str(page.parent.id),
        "snapshot_sequence": page.snapshot_sequence,
    }
    next_page_cursor = None
    if page.next_after_sequence is not None:
        next_page_cursor = _cursor(
            "replies-page",
            **cursor_values,
            after_sequence=page.next_after_sequence,
        )
    return {
        "parent_id": page.parent.id,
        "messages": [message_payload(item) for item in page.messages],
        "snapshot_cursor": _cursor("replies-snapshot", **cursor_values),
        "next_page_cursor": next_page_cursor,
        "complete": page.complete,
    }


def _replies(request: HttpRequest, payload: RepliesRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _reply_cursor_values(payload)
    try:
        page = list_replies(
            credential=credential,
            address=payload.address,
            message_id=payload.message_id,
            cycle_id=cursor.cycle_id,
            limit=payload.limit,
            after_sequence=cursor.after_sequence,
            snapshot_sequence=cursor.snapshot_sequence,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _reply_page_response(page)


@router.post(
    "/tree/replies",
    response={200: RepliesResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_replies(request: HttpRequest, payload: RepliesRequest) -> dict[str, Any]:
    return _replies(request, payload)


def _subtree_cursor_values(payload: SubtreeRequest) -> PageCursor:
    cycle_id = payload.cycle_id
    message_id = payload.message_id
    max_depth = payload.max_depth
    after_sequence = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None
    if payload.snapshot_cursor:
        decoded = _decoded_cursor(payload.snapshot_cursor, kind="subtree-snapshot")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        cursor_message = _cursor_uuid(decoded, "message_id")
        cursor_depth = _cursor_int(decoded, "max_depth")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and snapshot cursor do not match.")
        if message_id != cursor_message or max_depth != cursor_depth:
            raise InvalidRequest("The subtree request and snapshot cursor do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = _cursor_int(decoded, "snapshot_sequence")
        expected_tunnel_id = _cursor_binding(decoded)
    if payload.page_cursor:
        decoded = _decoded_cursor(payload.page_cursor, kind="subtree-page")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        cursor_message = _cursor_uuid(decoded, "message_id")
        cursor_depth = _cursor_int(decoded, "max_depth")
        page_snapshot = _cursor_int(decoded, "snapshot_sequence")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and page cursor do not match.")
        if message_id != cursor_message or max_depth != cursor_depth:
            raise InvalidRequest("The subtree request and page cursor do not match.")
        if snapshot_sequence is not None and snapshot_sequence != page_snapshot:
            raise InvalidRequest("The page and snapshot cursors do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = page_snapshot
        after_sequence = _cursor_int(decoded, "after_sequence")
        page_binding = _cursor_binding(decoded)
        if expected_tunnel_id is not None and page_binding != expected_tunnel_id:
            raise InvalidCursor()
        expected_tunnel_id = page_binding
    return PageCursor(
        cycle_id=cycle_id,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        expected_tunnel_id=expected_tunnel_id,
    )


def _subtree_response(page: SubtreePage, *, max_depth: int) -> dict[str, Any]:
    snapshot_cursor = _cursor(
        "subtree-snapshot",
        tunnel_id=str(page.tunnel.id),
        cycle_id=str(page.cycle.id),
        message_id=str(page.root.id),
        max_depth=max_depth,
        snapshot_sequence=page.snapshot_sequence,
    )
    next_page_cursor = None
    if page.next_after_sequence is not None:
        next_page_cursor = _cursor(
            "subtree-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            message_id=str(page.root.id),
            max_depth=max_depth,
            snapshot_sequence=page.snapshot_sequence,
            after_sequence=page.next_after_sequence,
        )
    return {
        "cycle_id": page.cycle.id,
        "root_id": page.root.id,
        "nodes": [message_payload(message) for message in page.messages],
        "snapshot_cursor": snapshot_cursor,
        "next_page_cursor": next_page_cursor,
        "truncated": page.truncated,
    }


def _subtree(request: HttpRequest, payload: SubtreeRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _subtree_cursor_values(payload)
    try:
        page = get_subtree(
            credential=credential,
            address=payload.address,
            cycle_id=cursor.cycle_id,
            message_id=payload.message_id,
            max_depth=payload.max_depth,
            limit=payload.limit,
            after_sequence=cursor.after_sequence,
            snapshot_sequence=cursor.snapshot_sequence,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _subtree_response(page, max_depth=payload.max_depth)


@router.post(
    "/tree/subtree",
    response={200: SubtreeResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_subtree(request: HttpRequest, payload: SubtreeRequest) -> dict[str, Any]:
    return _subtree(request, payload)


def _leaves_cursor_values(payload: LeavesRequest) -> PageCursor:
    cycle_id = payload.cycle_id
    after_sequence = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None
    if payload.snapshot_cursor:
        decoded = _decoded_cursor(payload.snapshot_cursor, kind="leaves-snapshot")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and snapshot cursor do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = _cursor_int(decoded, "snapshot_sequence")
        expected_tunnel_id = _cursor_binding(decoded)
    if payload.page_cursor:
        decoded = _decoded_cursor(payload.page_cursor, kind="leaves-page")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        page_snapshot = _cursor_int(decoded, "snapshot_sequence")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and page cursor do not match.")
        if snapshot_sequence is not None and snapshot_sequence != page_snapshot:
            raise InvalidRequest("The page and snapshot cursors do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = page_snapshot
        after_sequence = _cursor_int(decoded, "after_sequence")
        page_binding = _cursor_binding(decoded)
        if expected_tunnel_id is not None and page_binding != expected_tunnel_id:
            raise InvalidCursor()
        expected_tunnel_id = page_binding
    return PageCursor(
        cycle_id=cycle_id,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        expected_tunnel_id=expected_tunnel_id,
    )


def _leaves_response(page: LeafPage) -> dict[str, Any]:
    snapshot_cursor = _cursor(
        "leaves-snapshot",
        tunnel_id=str(page.tunnel.id),
        cycle_id=str(page.cycle.id),
        snapshot_sequence=page.snapshot_sequence,
    )
    next_page_cursor = None
    if page.next_after_sequence is not None:
        next_page_cursor = _cursor(
            "leaves-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            after_sequence=page.next_after_sequence,
        )
    return {
        "cycle_id": page.cycle.id,
        "leaves": [message_payload(message) for message in page.messages],
        "snapshot_cursor": snapshot_cursor,
        "next_page_cursor": next_page_cursor,
        "complete": page.complete,
    }


def _leaves(request: HttpRequest, payload: LeavesRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _leaves_cursor_values(payload)
    try:
        page = list_leaves(
            credential=credential,
            address=payload.address,
            cycle_id=cursor.cycle_id,
            limit=payload.limit,
            after_sequence=cursor.after_sequence,
            snapshot_sequence=cursor.snapshot_sequence,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _leaves_response(page)


@router.post(
    "/tree/leaves",
    response={200: LeavesResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_leaves(request: HttpRequest, payload: LeavesRequest) -> dict[str, Any]:
    return _leaves(request, payload)


def _activity_event(event: Any) -> dict[str, Any]:
    return {
        "position": event.position,
        "type": event.event_type,
        "occurred_at": event.occurred_at,
        "cycle_id": event.cycle_id,
        "message": message_payload(event.message) if event.message is not None else None,
    }


async def _activity(request: HttpRequest, payload: ActivityRequest) -> dict[str, Any]:
    credential = _credential(request)
    expected_tunnel_id: UUID | None = None
    after_position: int | None = None
    if payload.after_cursor:
        expected_tunnel_id, after_position = _activity_cursor_values(payload.after_cursor)
    try:
        page = await read_activity_async(
            credential=credential,
            address=payload.address,
            after_position=after_position,
            wait_seconds=payload.wait_seconds,
            limit=payload.limit,
            event_types=tuple(payload.event_types) if payload.event_types is not None else None,
        )
    except TunnelUnavailable:
        await sync_to_async(_record_miss, thread_sensitive=True)(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=expected_tunnel_id,
    )
    return {
        "events": [_activity_event(event) for event in page.events],
        "next_cursor": _activity_cursor(page.tunnel, page.next_position),
        "has_more": page.has_more,
    }


@router.post(
    "/activity",
    response={
        200: ActivityResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
async def read_tunnel_activity(request: HttpRequest, payload: ActivityRequest) -> dict[str, Any]:
    return await _activity(request, payload)


def _checkpoint(request: HttpRequest, payload: CheckpointRequest) -> dict[str, Any]:
    credential = _credential(request)
    expected_tunnel_id, position = _activity_cursor_values(payload.cursor)
    try:
        checkpoint, advanced = commit_checkpoint(
            credential=credential,
            address=payload.address,
            position=position,
            expected_tunnel_id=expected_tunnel_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return {
        "cursor": _activity_cursor(checkpoint.tunnel, checkpoint.last_event_position),
        "position": checkpoint.last_event_position,
        "updated_at": checkpoint.updated_at,
        "advanced": advanced,
    }


@router.post(
    "/activity/checkpoint",
    response={
        200: CheckpointResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def commit_tunnel_checkpoint(request: HttpRequest, payload: CheckpointRequest) -> dict[str, Any]:
    return _checkpoint(request, payload)


def _context_cursor_values(payload: ContextRequest) -> ContextCursor:
    cycle_id = payload.cycle_id
    snapshot_sequence: int | None = None
    activity_position: int | None = None
    expected_tunnel_id: UUID | None = None
    after_position: int | None = None
    if payload.snapshot_cursor:
        decoded = _decoded_cursor(payload.snapshot_cursor, kind="context-snapshot")
        cursor_cycle = _cursor_uuid(decoded, "cycle_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and context snapshot do not match.")
        cycle_id = cursor_cycle
        expected_tunnel_id = _cursor_binding(decoded)
        snapshot_sequence = _cursor_int(decoded, "snapshot_sequence")
        activity_position = _cursor_int(decoded, "activity_position")
    if payload.after_cursor:
        activity_tunnel_id, after_position = _activity_cursor_values(payload.after_cursor)
        if expected_tunnel_id and activity_tunnel_id != expected_tunnel_id:
            raise TunnelUnavailable()
        expected_tunnel_id = activity_tunnel_id
    return ContextCursor(
        cycle_id=cycle_id,
        snapshot_sequence=snapshot_sequence,
        activity_position=activity_position,
        expected_tunnel_id=expected_tunnel_id,
        after_position=after_position,
    )


def _context(request: HttpRequest, payload: ContextRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _context_cursor_values(payload)
    try:
        view = get_context(
            credential=credential,
            address=payload.address,
            cycle_id=cursor.cycle_id,
            focus_message_id=payload.focus_message_id,
            snapshot_sequence=cursor.snapshot_sequence,
            activity_position=cursor.activity_position,
            include_branch=payload.include_branch,
            include_direct_replies=payload.include_direct_replies,
            include_recent_activity=payload.include_recent_activity,
            referenced_message_ids=tuple(payload.referenced_message_ids),
            after_activity_position=cursor.after_position,
            max_items=payload.max_items,
            max_bytes=payload.max_bytes,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=view.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    snapshot_cursor = _cursor(
        "context-snapshot",
        tunnel_id=str(view.tunnel.id),
        cycle_id=str(view.cycle_id),
        snapshot_sequence=view.snapshot_sequence,
        activity_position=view.activity_position,
    )
    return {
        "cycle_id": view.cycle_id,
        "snapshot_cursor": snapshot_cursor,
        "items": [
            {"reason": item.reason, "message": message_payload(item.message)} for item in view.items
        ],
        "activity_cursor": _activity_cursor(view.tunnel, view.activity_position),
        "used_items": view.used_items,
        "used_bytes": view.used_bytes,
        "truncated": view.truncated,
    }


@router.post(
    "/context",
    response={
        200: ContextResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def read_tunnel_context(request: HttpRequest, payload: ContextRequest) -> dict[str, Any]:
    return _context(request, payload)


def _search_filter_digest(payload: SearchRequest) -> str:
    values = {
        "query": (payload.query or "").strip(),
        "sender_id": str(payload.sender_id) if payload.sender_id else None,
        "mentioned_agent_id": (
            str(payload.mentioned_agent_id) if payload.mentioned_agent_id else None
        ),
        "correlation_id": (
            payload.correlation_id.strip() if payload.correlation_id is not None else None
        ),
        "branch_root_id": str(payload.branch_root_id) if payload.branch_root_id else None,
        "sequence_after": payload.sequence_after,
        "sequence_before": payload.sequence_before,
    }
    canonical = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _search_page_values(payload: SearchRequest) -> SearchCursor:
    if payload.page_cursor is None:
        return SearchCursor(cycle_id=payload.cycle_id)
    decoded = _decoded_cursor(payload.page_cursor, kind="search-page")
    if decoded.get("filters") != _search_filter_digest(payload):
        raise InvalidCursor()
    cycle_id = _cursor_uuid(decoded, "cycle_id")
    if payload.cycle_id is not None and payload.cycle_id != cycle_id:
        raise InvalidCursor()
    tunnel_id = _cursor_binding(decoded)
    return SearchCursor(
        cycle_id=cycle_id,
        snapshot_sequence=_cursor_int(decoded, "snapshot_sequence"),
        after_rank=_cursor_int(decoded, "rank"),
        after_sequence=_cursor_int(decoded, "sequence"),
        after_message_id=_cursor_uuid(decoded, "message_id"),
        expected_tunnel_id=tunnel_id,
    )


def _search(request: HttpRequest, payload: SearchRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _search_page_values(payload)
    try:
        page = search_messages(
            credential=credential,
            address=payload.address,
            query=payload.query,
            cycle_id=cursor.cycle_id,
            sender_id=payload.sender_id,
            mentioned_agent_id=payload.mentioned_agent_id,
            correlation_id=payload.correlation_id,
            branch_root_id=payload.branch_root_id,
            sequence_after=payload.sequence_after,
            sequence_before=payload.sequence_before,
            snapshot_sequence=cursor.snapshot_sequence,
            after_rank=cursor.after_rank,
            after_sequence=cursor.after_sequence,
            after_message_id=cursor.after_message_id,
            limit=payload.limit,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    next_cursor = None
    if (
        page.next_after_rank is not None
        and page.next_after_sequence is not None
        and page.next_after_message_id is not None
    ):
        next_cursor = _cursor(
            "search-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            filters=_search_filter_digest(payload),
            rank=page.next_after_rank,
            sequence=page.next_after_sequence,
            message_id=str(page.next_after_message_id),
        )
    return {
        "cycle_id": page.cycle.id,
        "snapshot_sequence": page.snapshot_sequence,
        "results": [
            {
                "message_id": hit.message.id,
                "cycle_id": hit.message.cycle_id,
                "parent_id": hit.message.parent_id,
                "sender": {"id": hit.message.sender_id, "name": hit.message.sender_name},
                "mention_ids": sorted(
                    (mention.credential_id for mention in hit.message.mentions.all()),
                    key=str,
                ),
                "correlation_id": hit.message.correlation_id or None,
                "sequence": hit.message.sequence,
                "depth": hit.message.depth,
                "created_at": hit.message.created_at,
                "rank": hit.rank,
                "snippet": hit.snippet,
            }
            for hit in page.hits
        ],
        "next_page_cursor": next_cursor,
        "complete": page.complete,
        "used_bytes": page.used_bytes,
    }


@router.post(
    "/search",
    response={
        200: SearchResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def search_tunnel_messages(request: HttpRequest, payload: SearchRequest) -> dict[str, Any]:
    return _search(request, payload)


def _participant_page_values(payload: ParticipantsRequest) -> ParticipantCursor:
    if payload.page_cursor is None:
        return ParticipantCursor(cycle_id=payload.cycle_id)
    decoded = _decoded_cursor(payload.page_cursor, kind="participants-page")
    cycle_id = _cursor_uuid(decoded, "cycle_id")
    if payload.cycle_id is not None and payload.cycle_id != cycle_id:
        raise InvalidCursor()
    tunnel_id = _cursor_binding(decoded)
    return ParticipantCursor(
        cycle_id=cycle_id,
        snapshot_sequence=_cursor_int(decoded, "snapshot_sequence"),
        after_first_sequence=_cursor_int(decoded, "first_sequence"),
        after_credential_id=_cursor_uuid(decoded, "credential_id"),
        expected_tunnel_id=tunnel_id,
    )


def _participants(request: HttpRequest, payload: ParticipantsRequest) -> dict[str, Any]:
    credential = _credential(request)
    cursor = _participant_page_values(payload)
    try:
        page = list_participants(
            credential=credential,
            address=payload.address,
            cycle_id=cursor.cycle_id,
            snapshot_sequence=cursor.snapshot_sequence,
            after_first_sequence=cursor.after_first_sequence,
            after_credential_id=cursor.after_credential_id,
            limit=payload.limit,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    next_cursor = None
    if page.next_after_first_sequence is not None and page.next_after_credential_id is not None:
        next_cursor = _cursor(
            "participants-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            first_sequence=page.next_after_first_sequence,
            credential_id=str(page.next_after_credential_id),
        )
    return {
        "cycle_id": page.cycle.id,
        "snapshot_sequence": page.snapshot_sequence,
        "participants": [
            {
                "agent_id": item.credential_id,
                "name_snapshot": item.name_snapshot,
                "message_count": item.message_count,
                "mention_count": item.mention_count,
                "first_sequence": item.first_sequence,
                "last_sequence": item.last_sequence,
            }
            for item in page.participants
        ],
        "next_page_cursor": next_cursor,
        "complete": page.complete,
    }


@router.post(
    "/participants",
    response={
        200: ParticipantsResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def list_tunnel_participants(request: HttpRequest, payload: ParticipantsRequest) -> dict[str, Any]:
    return _participants(request, payload)


def _tunnel_status(request: HttpRequest, payload: TunnelStatusRequest) -> dict[str, Any]:
    credential = _credential(request)
    try:
        status = get_tunnel_status(
            credential=credential,
            address=payload.address,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    tunnel = status.tunnel
    return {
        "id": tunnel.id,
        "label": tunnel.label,
        "state": tunnel.state,
        "address_generation": status.current_address.generation,
        "next_cycle_number": tunnel.next_cycle_number,
        "latest_event_position": tunnel.next_event_position - 1,
        "created_at": tunnel.created_at,
        "dormant_at": tunnel.dormant_at,
        "current_cycle": (
            _directory_cycle(status.current_cycle) if status.current_cycle is not None else None
        ),
    }


@router.post(
    "/tunnel/status",
    response={
        200: TunnelStatusResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def tunnel_status(request: HttpRequest, payload: TunnelStatusRequest) -> dict[str, Any]:
    return _tunnel_status(request, payload)


def _cycle_list(request: HttpRequest, payload: CycleListRequest) -> dict[str, Any]:
    snapshot_number: int | None = None
    before_number: int | None = None
    expected_tunnel_id: UUID | None = None
    if payload.page_cursor:
        decoded = _decoded_cursor(payload.page_cursor, kind="cycle-page")
        snapshot_number = _cursor_int(decoded, "snapshot_number")
        before_number = _cursor_int(decoded, "before_number")
        expected_tunnel_id = _cursor_binding(decoded)
    credential = _credential(request)
    try:
        page = list_cycles(
            credential=credential,
            address=payload.address,
            limit=payload.limit,
            before_number=before_number,
            snapshot_number=snapshot_number,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=expected_tunnel_id,
    )
    binding = {
        "tunnel_id": str(page.tunnel.id),
        "snapshot_number": page.snapshot_number,
    }
    next_cursor = (
        _cursor("cycle-page", **binding, before_number=page.next_before_number)
        if page.next_before_number is not None
        else None
    )
    return {
        "items": [_cycle_directory_item(cycle) for cycle in page.cycles],
        "snapshot_cursor": _cursor("cycle-snapshot", **binding),
        "next_page_cursor": next_cursor,
        "complete": page.complete,
    }


@router.post(
    "/cycles",
    response={
        200: CycleListResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def list_tunnel_cycles(request: HttpRequest, payload: CycleListRequest) -> dict[str, Any]:
    return _cycle_list(request, payload)


def _start_cycle(
    request: HttpRequest,
    payload: StartCycleRequest,
    idempotency_key: str,
) -> dict[str, Any]:
    credential = _credential(request)
    root = payload.cycle.root
    try:
        started = start_cycle(
            credential=credential,
            idempotency_key=idempotency_key,
            address=payload.address,
            expected_address_generation=payload.expected_address_generation,
            cycle_label=payload.cycle.label,
            expires_in_seconds=payload.cycle.expires_in_seconds,
            root_content=root.content.model_dump(mode="json"),
            root_mentions=root.mentions,
            root_correlation_id=root.correlation_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return _started_cycle_response(started)


@router.post(
    "/cycles/start",
    response={
        201: StartedCycleResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def start_tunnel_cycle(
    request: HttpRequest,
    payload: StartCycleRequest,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> Status[dict[str, Any]]:
    return Status(201, _start_cycle(request, payload, idempotency_key))


def _rollover_cycle(
    request: HttpRequest,
    payload: RolloverCycleRequest,
    idempotency_key: str,
) -> dict[str, Any]:
    credential = _credential(request)
    root = payload.cycle.root
    try:
        started = rollover_cycle(
            credential=credential,
            idempotency_key=idempotency_key,
            address=payload.address,
            expected_cycle_id=payload.expected_cycle_id,
            expected_address_generation=payload.expected_address_generation,
            cycle_label=payload.cycle.label,
            expires_in_seconds=payload.cycle.expires_in_seconds,
            root_content=root.content.model_dump(mode="json"),
            root_mentions=root.mentions,
            root_correlation_id=root.correlation_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return _started_cycle_response(started)


@router.post(
    "/cycles/rollover",
    response={
        201: StartedCycleResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def rollover_tunnel_cycle(
    request: HttpRequest,
    payload: RolloverCycleRequest,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> Status[dict[str, Any]]:
    return Status(201, _rollover_cycle(request, payload, idempotency_key))


def _close_cycle(
    request: HttpRequest,
    payload: CloseCycleRequest,
    idempotency_key: str,
) -> HttpResponse:
    credential = _credential(request)
    try:
        close_cycle(
            credential=credential,
            idempotency_key=idempotency_key,
            address=payload.address,
            expected_cycle_id=payload.expected_cycle_id,
        )
    except TunnelUnavailable:
        _record_miss(request, credential)
        raise
    return HttpResponse(status=204)


@router.post(
    "/cycles/close",
    response={
        204: None,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def close_tunnel_cycle(
    request: HttpRequest,
    payload: CloseCycleRequest,
    idempotency_key: HeaderEx[
        str,
        P(alias="Idempotency-Key", min_length=8, max_length=128, pattern=r"^[\x20-\x7e]+$"),
    ],
) -> HttpResponse:
    return _close_cycle(request, payload, idempotency_key)


api.add_router("/v1", router)
