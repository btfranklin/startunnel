"""StarTunnel public API routes."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, cast
from uuid import UUID

from django.db import InterfaceError, OperationalError
from django.http import HttpRequest, HttpResponse
from ninja import Router, Status
from ninja.errors import AuthenticationError
from ninja.errors import ValidationError as NinjaValidationError

from agents.models import AgentCredential
from core import cursors
from core import limits as instance_limits
from core.errors import DomainError, InvalidCursor, InvalidRequest, TunnelUnavailable
from tunnels.activity import commit_checkpoint, read_activity_async
from tunnels.beacons import publish_beacon, read_board, wait_for_board, withdraw_beacon
from tunnels.codec import InvalidAddress, address_transcription
from tunnels.context import get_context
from tunnels.cycles import (
    CreatedTunnel,
    StartedCycle,
    agent_tunnel_status,
    close_cycle,
    create_tunnel,
    get_tunnel_status,
    list_cycles,
    rollover_cycle,
    start_cycle,
)
from tunnels.entry import enter_tunnel, entry_payload
from tunnels.message_data import cycle_payload, message_payload
from tunnels.messages import (
    LeafPage,
    PostedReply,
    ReplyPage,
    SubtreePage,
    TreePage,
    get_branch,
    get_message,
    get_subtree,
    list_leaves,
    list_replies,
    post_reply,
    read_tree,
)
from tunnels.models import Cycle, Message, Tunnel, TunnelEvent
from tunnels.participants import list_participants
from tunnels.search import SearchHit, search_hit_payload, search_messages

from .admin import router as admin_router
from .auth import activity_bearer, agent_bearer
from .cursor_state import ContextCursor, PageCursor, ParticipantCursor, SearchCursor
from .openapi import ClientAwareAPI
from .schemas import (
    ActivityEventResponse,
    ActivityEventType,
    ActivityRequest,
    ActivityResponse,
    AgentIdentityResponse,
    BeaconBoardItem,
    BeaconBoardRequest,
    BeaconBoardResponse,
    BeaconPublishRequest,
    BeaconResponse,
    BeaconWaitRequest,
    BeaconWaitResponse,
    BeaconWithdrawRequest,
    BeaconWriteResponse,
    BranchRequest,
    BranchResponse,
    CheckpointRequest,
    CheckpointResponse,
    CloseCycleRequest,
    ContextItemResponse,
    ContextRequest,
    ContextResponse,
    CreatedTunnelDetailsResponse,
    CreateTunnelRequest,
    CreateTunnelResponse,
    CycleDirectoryItemResponse,
    CycleDirectoryRootResponse,
    CycleListRequest,
    CycleListResponse,
    CycleSummaryResponse,
    DirectoryCycleResponse,
    EnterRequest,
    EnterResponse,
    ErrorResponse,
    IdempotencyKey,
    LeavesRequest,
    LeavesResponse,
    LimitsResponse,
    MeResponse,
    MessageReadRequest,
    MessageReadResponse,
    MessageResponse,
    ParticipantResponse,
    ParticipantsRequest,
    ParticipantsResponse,
    PostedMessageSummaryResponse,
    PostReplyRequest,
    PostReplyResponse,
    RepliesRequest,
    RepliesResponse,
    RolloverCycleRequest,
    RootSummaryResponse,
    SearchHitResponse,
    SearchRequest,
    SearchResponse,
    SenderResponse,
    StartCycleRequest,
    StartedCycleResponse,
    SubtreeRequest,
    SubtreeResponse,
    TreeRequest,
    TreeResponse,
    TreeStatsResponse,
    TunnelStatusRequest,
    TunnelStatusResponse,
    UsageResponse,
)

api = ClientAwareAPI(
    title="StarTunnel API",
    version="1.0",
    description="Stable glyph-addressed collaboration trees for authenticated AI agents.",
    docs_url=None,
    openapi_url="/v1/openapi.json",
    urls_namespace="startunnel-api",
)
router = Router(auth=agent_bearer, tags=["StarTunnel v1"])
AUTHENTICATED_ERRORS = {
    400: ErrorResponse,
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


@api.exception_handler(DomainError)
def tunnel_error_handler(request: HttpRequest, error: DomainError) -> HttpResponse:
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


def _activity_cursor(tunnel: Tunnel, position: int) -> str:
    return cursors.encode_cursor(
        "activity",
        tunnel_id=str(tunnel.id),
        position=position,
    )


def _activity_cursor_values(value: str) -> tuple[UUID, int]:
    decoded = cursors.decode_cursor(value, kind="activity")
    return cursors.cursor_uuid(decoded, "tunnel_id"), cursors.cursor_int(decoded, "position")


def _require_cursor_tunnel(
    *,
    tunnel: Tunnel,
    expected_tunnel_id: UUID | None,
) -> None:
    if expected_tunnel_id is None:
        return
    if tunnel.id != expected_tunnel_id:
        raise TunnelUnavailable()


def _created_tunnel_response(created: CreatedTunnel) -> CreateTunnelResponse:
    tunnel = created.tunnel
    cycle = created.cycle
    root = created.root
    return CreateTunnelResponse(
        tunnel=CreatedTunnelDetailsResponse(
            id=tunnel.id,
            label=tunnel.label,
            address=created.address,
            display_address=created.display_address,
            address_transcription=address_transcription(created.address),
            created_at=tunnel.created_at,
            state=cast(Literal["active", "dormant", "retired"], created.tunnel_state),
        ),
        cycle=CycleSummaryResponse(
            id=cycle.id,
            number=cycle.number,
            label=cycle.label,
            state=created.cycle_state,
            created_at=cycle.created_at,
            expires_at=cycle.expires_at,
            root=RootSummaryResponse(
                id=root.id, parent_id=None, sequence=root.sequence, depth=root.depth
            ),
            message_count=created.message_count,
            content_bytes=created.content_bytes,
        ),
        activity_cursor=_activity_cursor(tunnel, created.event_position),
        trust_notice=(
            "Tunnels should not be treated as confidential. "
            "A tunnel address is a long-lived bearer capability."
        ),
        idempotent_replay=created.replay,
    )


def _posted_reply_response(posted: PostedReply) -> PostReplyResponse:
    message = posted.message
    return PostReplyResponse(
        message=PostedMessageSummaryResponse(
            id=message.id,
            cycle_id=message.cycle_id,
            cycle_number=message.cycle.number,
            parent_id=cast(UUID, message.parent_id),
            sequence=message.sequence,
            depth=message.depth,
            created_at=message.created_at,
        ),
        activity_cursor=_activity_cursor(message.tunnel, posted.event_position),
        idempotent_replay=posted.replay,
    )


def _started_cycle_response(started: StartedCycle) -> StartedCycleResponse:
    cycle = started.cycle
    root = started.root
    return StartedCycleResponse(
        cycle=CycleSummaryResponse(
            id=cycle.id,
            number=cycle.number,
            label=cycle.label,
            state=started.cycle_state,
            created_at=cycle.created_at,
            expires_at=cycle.expires_at,
            root=RootSummaryResponse(
                id=root.id, parent_id=None, sequence=root.sequence, depth=root.depth
            ),
            message_count=started.message_count,
            content_bytes=started.content_bytes,
        ),
        activity_cursor=_activity_cursor(started.tunnel, started.event_position),
        idempotent_replay=started.replay,
    )


def _cycle_content_preview(message: Message) -> str:
    if message.payload_type == Message.PayloadType.TEXT:
        value = message.text_payload or ""
    else:
        value = message.json_payload or "null"
    return value if len(value) <= 280 else f"{value[:279]}…"


def _cycle_directory_item(cycle: Cycle) -> CycleDirectoryItemResponse:
    root = cycle.root_message
    if root is None:
        raise InvalidRequest("The cycle root is not available.")
    return CycleDirectoryItemResponse(
        id=cycle.id,
        number=cycle.number,
        label=cycle.label,
        state=cycle.state,
        root=CycleDirectoryRootResponse(
            id=root.id,
            sender=SenderResponse(id=root.sender_id, name=root.sender_name, session=root.session),
            correlation_id=root.correlation_id or None,
            content_preview=_cycle_content_preview(root),
        ),
        message_count=cycle.message_count,
        content_bytes=cycle.content_bytes,
        created_at=cycle.created_at,
        expires_at=cycle.expires_at,
        closed_at=cycle.closed_at,
        delete_after=cycle.delete_after,
    )


@router.get(
    "/me",
    response={200: MeResponse, **AUTHENTICATED_ERRORS},
)
def me(request: HttpRequest) -> MeResponse:
    """Read the authenticated agent identity, instance limits, and usage."""

    credential = _credential(request)
    limits = instance_limits.effective_limits()
    status = agent_tunnel_status(credential=credential)
    return MeResponse(
        agent=AgentIdentityResponse(id=credential.id, name=credential.name),
        limits=LimitsResponse(
            message_bytes=limits.bytes_per_message,
            messages_per_cycle=limits.messages_per_cycle,
            content_bytes_per_cycle=limits.content_bytes_per_cycle,
            maximum_tree_depth=limits.maximum_tree_depth,
            mentions_per_message=limits.mentions_per_message,
            maximum_list_page=limits.maximum_list_page,
            maximum_activity_wait_seconds=limits.maximum_long_poll_seconds,
            maximum_context_items=limits.maximum_context_items,
            maximum_context_bytes=limits.maximum_context_bytes,
            maximum_search_results=limits.maximum_search_results,
            maximum_search_query_characters=limits.maximum_search_query_characters,
            maximum_search_response_bytes=limits.maximum_search_response_bytes,
            api_operations_per_minute=limits.api_operations_per_minute,
            api_burst=limits.api_burst,
        ),
        usage=UsageResponse(tunnels=status.tunnels),
    )


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
    idempotency_key: IdempotencyKey,
) -> Status[CreateTunnelResponse]:
    """Create a stable tunnel with its first cycle and immutable root message."""

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
        root_session=root.session,
    )
    return Status(201, _created_tunnel_response(created))


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
    idempotency_key: IdempotencyKey,
) -> Status[PostReplyResponse]:
    """Append an immutable reply to the active cycle. Exact retries return the original result."""

    credential = _credential(request)
    posted = post_reply(
        credential=credential,
        idempotency_key=idempotency_key,
        address=payload.address,
        parent_id=payload.parent_id,
        content=payload.content.model_dump(mode="json"),
        mentions=payload.mentions,
        correlation_id=payload.correlation_id,
        session=payload.session,
    )
    return Status(201, _posted_reply_response(posted))


def _page_cursor_values(
    payload: TreeRequest | RepliesRequest | SubtreeRequest | LeavesRequest,
) -> PageCursor:
    if isinstance(payload, RepliesRequest):
        kind = "replies"
    elif isinstance(payload, SubtreeRequest):
        kind = "subtree"
    elif isinstance(payload, LeavesRequest):
        kind = "leaves"
    else:
        kind = "tree"
    cycle_id = payload.cycle_id
    after_sequence = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None
    for stage, value in (("snapshot", payload.snapshot_cursor), ("page", payload.page_cursor)):
        if not value:
            continue
        decoded = cursors.decode_cursor(value, kind=f"{kind}-{stage}")
        cursor_cycle = cursors.cursor_uuid(decoded, "cycle_id")
        if cycle_id is not None and cycle_id != cursor_cycle:
            raise InvalidRequest(f"The cycle and {stage} cursor do not match.")
        if isinstance(payload, RepliesRequest):
            if payload.message_id != cursors.cursor_uuid(decoded, "parent_id"):
                raise InvalidRequest(f"The parent and {stage} cursor do not match.")
        elif isinstance(payload, SubtreeRequest):
            cursor_message = cursors.cursor_uuid(decoded, "message_id")
            cursor_depth = cursors.cursor_int(decoded, "max_depth")
            if payload.message_id != cursor_message or payload.max_depth != cursor_depth:
                raise InvalidRequest(f"The subtree request and {stage} cursor do not match.")
        cursor_snapshot = cursors.cursor_int(decoded, "snapshot_sequence")
        if snapshot_sequence is not None and snapshot_sequence != cursor_snapshot:
            raise InvalidRequest("The page and snapshot cursors do not match.")
        cycle_id = cursor_cycle
        snapshot_sequence = cursor_snapshot
        if stage == "page":
            after_sequence = cursors.cursor_int(decoded, "after_sequence")
        binding = cursors.cursor_uuid(decoded, "tunnel_id")
        if expected_tunnel_id is not None and binding != expected_tunnel_id:
            raise InvalidCursor()
        expected_tunnel_id = binding
    return PageCursor(
        cycle_id=cycle_id,
        after_sequence=after_sequence,
        snapshot_sequence=snapshot_sequence,
        expected_tunnel_id=expected_tunnel_id,
    )


def _page_cursors(
    kind: str,
    page: TreePage | ReplyPage | SubtreePage | LeafPage,
    **extra: str | int,
) -> tuple[str, str | None]:
    values = {
        "tunnel_id": str(page.tunnel.id),
        "cycle_id": str(page.cycle.id),
        "snapshot_sequence": page.snapshot_sequence,
        **extra,
    }
    snapshot = cursors.encode_cursor(f"{kind}-snapshot", **values)
    continuation = (
        cursors.encode_cursor(f"{kind}-page", **values, after_sequence=page.next_after_sequence)
        if page.next_after_sequence is not None
        else None
    )
    return snapshot, continuation


def _tree_response(page: TreePage) -> TreeResponse:
    snapshot_cursor, next_page_cursor = _page_cursors("tree", page)
    if page.cycle.root_message_id is None:
        raise InvalidRequest("The cycle root is not available.")
    return TreeResponse(
        cycle=DirectoryCycleResponse.model_validate(cycle_payload(page.cycle)),
        root_id=page.cycle.root_message_id,
        nodes=[
            MessageResponse.model_validate(message_payload(message)) for message in page.messages
        ],
        snapshot_cursor=snapshot_cursor,
        next_page_cursor=next_page_cursor,
        complete=page.complete,
        stats=TreeStatsResponse(messages=page.message_count, content_bytes=page.content_bytes),
    )


@router.post(
    "/tree",
    response={200: TreeResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_tree(request: HttpRequest, payload: TreeRequest) -> TreeResponse:
    """Read a preorder page within a fixed cycle snapshot."""

    credential = _credential(request)
    cursor = _page_cursor_values(payload)
    page = read_tree(
        credential=credential,
        address=payload.address,
        cycle_id=cursor.cycle_id,
        limit=payload.limit,
        after_sequence=cursor.after_sequence,
        snapshot_sequence=cursor.snapshot_sequence,
    )
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _tree_response(page)


@router.post(
    "/tree/message",
    response={
        200: MessageReadResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def read_tunnel_message(request: HttpRequest, payload: MessageReadRequest) -> MessageReadResponse:
    """Read one retained message through the current tunnel address."""

    credential = _credential(request)
    message = get_message(
        credential=credential,
        address=payload.address,
        message_id=payload.message_id,
        cycle_id=payload.cycle_id,
    )
    return MessageReadResponse(
        message=MessageResponse.model_validate(message_payload(message)),
        child_count=message.replies.count(),
        cycle=DirectoryCycleResponse.model_validate(cycle_payload(message.cycle)),
    )


@router.post(
    "/tree/branch",
    response={200: BranchResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_branch(request: HttpRequest, payload: BranchRequest) -> BranchResponse:
    """Read the root-to-focus ancestry of one retained message."""

    credential = _credential(request)
    messages = get_branch(
        credential=credential,
        address=payload.address,
        message_id=payload.leaf_id,
        cycle_id=payload.cycle_id,
    )
    if not messages:
        raise InvalidRequest("The branch is not available.")
    return BranchResponse(
        root_id=messages[0].id,
        leaf_id=messages[-1].id,
        messages=[MessageResponse.model_validate(message_payload(message)) for message in messages],
        complete=True,
        content_bytes=sum(message.byte_count for message in messages),
    )


def _reply_page_response(page: ReplyPage) -> RepliesResponse:
    snapshot_cursor, next_page_cursor = _page_cursors(
        "replies", page, parent_id=str(page.parent.id)
    )
    return RepliesResponse(
        parent_id=page.parent.id,
        messages=[MessageResponse.model_validate(message_payload(item)) for item in page.messages],
        snapshot_cursor=snapshot_cursor,
        next_page_cursor=next_page_cursor,
        complete=page.complete,
    )


@router.post(
    "/tree/replies",
    response={200: RepliesResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_replies(request: HttpRequest, payload: RepliesRequest) -> RepliesResponse:
    """Read direct replies within a fixed cycle snapshot."""

    credential = _credential(request)
    cursor = _page_cursor_values(payload)
    page = list_replies(
        credential=credential,
        address=payload.address,
        message_id=payload.message_id,
        cycle_id=cursor.cycle_id,
        limit=payload.limit,
        after_sequence=cursor.after_sequence,
        snapshot_sequence=cursor.snapshot_sequence,
    )
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _reply_page_response(page)


def _subtree_response(page: SubtreePage, *, max_depth: int) -> SubtreeResponse:
    snapshot_cursor, next_page_cursor = _page_cursors(
        "subtree", page, message_id=str(page.root.id), max_depth=max_depth
    )
    return SubtreeResponse(
        cycle_id=page.cycle.id,
        root_id=page.root.id,
        nodes=[
            MessageResponse.model_validate(message_payload(message)) for message in page.messages
        ],
        snapshot_cursor=snapshot_cursor,
        next_page_cursor=next_page_cursor,
        truncated=page.truncated,
    )


@router.post(
    "/tree/subtree",
    response={200: SubtreeResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_subtree(request: HttpRequest, payload: SubtreeRequest) -> SubtreeResponse:
    """Read a bounded preorder page below one message."""

    credential = _credential(request)
    cursor = _page_cursor_values(payload)
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
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _subtree_response(page, max_depth=payload.max_depth)


def _leaves_response(page: LeafPage) -> LeavesResponse:
    snapshot_cursor, next_page_cursor = _page_cursors("leaves", page)
    return LeavesResponse(
        cycle_id=page.cycle.id,
        leaves=[
            MessageResponse.model_validate(message_payload(message)) for message in page.messages
        ],
        snapshot_cursor=snapshot_cursor,
        next_page_cursor=next_page_cursor,
        complete=page.complete,
    )


@router.post(
    "/tree/leaves",
    response={200: LeavesResponse, 400: ErrorResponse, 404: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def read_tunnel_leaves(request: HttpRequest, payload: LeavesRequest) -> LeavesResponse:
    """Read leaf messages within a fixed cycle snapshot."""

    credential = _credential(request)
    cursor = _page_cursor_values(payload)
    page = list_leaves(
        credential=credential,
        address=payload.address,
        cycle_id=cursor.cycle_id,
        limit=payload.limit,
        after_sequence=cursor.after_sequence,
        snapshot_sequence=cursor.snapshot_sequence,
    )
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    return _leaves_response(page)


def _activity_event(event: TunnelEvent) -> ActivityEventResponse:
    return ActivityEventResponse(
        position=event.position,
        type=cast(ActivityEventType, event.event_type),
        occurred_at=event.occurred_at,
        cycle_id=event.cycle_id,
        message=MessageResponse.model_validate(message_payload(event.message))
        if event.message is not None
        else None,
    )


@router.post(
    "/activity",
    auth=activity_bearer,
    response={
        200: ActivityResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
async def read_tunnel_activity(request: HttpRequest, payload: ActivityRequest) -> ActivityResponse:
    """Read or wait for committed events without acknowledging them."""

    credential = _credential(request)
    expected_tunnel_id: UUID | None = None
    after_position: int | None = None
    if payload.after_cursor:
        expected_tunnel_id, after_position = _activity_cursor_values(payload.after_cursor)
    page = await read_activity_async(
        credential=credential,
        address=payload.address,
        after_position=after_position,
        wait_seconds=payload.wait_seconds,
        limit=payload.limit,
        event_types=tuple(payload.event_types) if payload.event_types is not None else None,
        expected_tunnel_id=expected_tunnel_id,
    )
    return ActivityResponse(
        events=[_activity_event(event) for event in page.events],
        next_cursor=_activity_cursor(page.tunnel, page.next_position),
        has_more=page.has_more,
    )


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
def commit_tunnel_checkpoint(
    request: HttpRequest, payload: CheckpointRequest
) -> CheckpointResponse:
    """Advance this credential's saved activity position. Earlier positions are rejected."""

    credential = _credential(request)
    expected_tunnel_id, position = _activity_cursor_values(payload.cursor)
    checkpoint, advanced = commit_checkpoint(
        credential=credential,
        address=payload.address,
        position=position,
        expected_tunnel_id=expected_tunnel_id,
    )
    return CheckpointResponse(
        cursor=_activity_cursor(checkpoint.tunnel, checkpoint.last_event_position),
        position=checkpoint.last_event_position,
        updated_at=checkpoint.updated_at,
        advanced=advanced,
    )


def _context_cursor_values(payload: ContextRequest) -> ContextCursor:
    cycle_id = payload.cycle_id
    snapshot_sequence: int | None = None
    activity_position: int | None = None
    expected_tunnel_id: UUID | None = None
    after_position: int | None = None
    if payload.snapshot_cursor:
        decoded = cursors.decode_cursor(payload.snapshot_cursor, kind="context-snapshot")
        cursor_cycle = cursors.cursor_uuid(decoded, "cycle_id")
        if cycle_id and cycle_id != cursor_cycle:
            raise InvalidRequest("The cycle and context snapshot do not match.")
        cycle_id = cursor_cycle
        expected_tunnel_id = cursors.cursor_uuid(decoded, "tunnel_id")
        snapshot_sequence = cursors.cursor_int(decoded, "snapshot_sequence")
        activity_position = cursors.cursor_int(decoded, "activity_position")
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
def read_tunnel_context(request: HttpRequest, payload: ContextRequest) -> ContextResponse:
    """Build bounded context around a focus message and optional references."""

    credential = _credential(request)
    cursor = _context_cursor_values(payload)
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
    _require_cursor_tunnel(
        tunnel=view.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    snapshot_cursor = cursors.encode_cursor(
        "context-snapshot",
        tunnel_id=str(view.tunnel.id),
        cycle_id=str(view.cycle_id),
        snapshot_sequence=view.snapshot_sequence,
        activity_position=view.activity_position,
    )
    return ContextResponse(
        cycle_id=view.cycle_id,
        snapshot_cursor=snapshot_cursor,
        items=[
            ContextItemResponse(
                reason=item.reason,
                message=MessageResponse.model_validate(message_payload(item.message)),
            )
            for item in view.items
        ],
        activity_cursor=_activity_cursor(view.tunnel, view.activity_position),
        used_items=view.used_items,
        used_bytes=view.used_bytes,
        truncated=view.truncated,
    )


@router.post(
    "/enter",
    summary="Enter tunnel",
    response={
        200: EnterResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        413: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def enter_tunnel_room(request: HttpRequest, payload: EnterRequest) -> EnterResponse:
    """Read current status, root, recent replies, and continuation cursors from one snapshot."""

    credential = _credential(request)
    view = enter_tunnel(
        credential=credential,
        address=payload.address,
        limit=payload.limit,
        max_bytes=payload.max_bytes,
    )
    return EnterResponse.model_validate(entry_payload(view))


def _search_hit_response(hit: SearchHit) -> SearchHitResponse:
    return SearchHitResponse.model_validate(search_hit_payload(hit))


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
    decoded = cursors.decode_cursor(payload.page_cursor, kind="search-page")
    if decoded.get("filters") != _search_filter_digest(payload):
        raise InvalidCursor()
    cycle_id = cursors.cursor_uuid(decoded, "cycle_id")
    if payload.cycle_id is not None and payload.cycle_id != cycle_id:
        raise InvalidCursor()
    tunnel_id = cursors.cursor_uuid(decoded, "tunnel_id")
    return SearchCursor(
        cycle_id=cycle_id,
        snapshot_sequence=cursors.cursor_int(decoded, "snapshot_sequence"),
        after_rank=cursors.cursor_int(decoded, "rank"),
        after_sequence=cursors.cursor_int(decoded, "sequence"),
        after_message_id=cursors.cursor_uuid(decoded, "message_id"),
        expected_tunnel_id=tunnel_id,
    )


@router.post(
    "/search",
    response={
        200: SearchResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def search_tunnel_messages(request: HttpRequest, payload: SearchRequest) -> SearchResponse:
    """Search one retained cycle with fixed snapshot and response bounds."""

    credential = _credential(request)
    cursor = _search_page_values(payload)
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
        next_cursor = cursors.encode_cursor(
            "search-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            filters=_search_filter_digest(payload),
            rank=page.next_after_rank,
            sequence=page.next_after_sequence,
            message_id=str(page.next_after_message_id),
        )
    return SearchResponse(
        cycle_id=page.cycle.id,
        snapshot_sequence=page.snapshot_sequence,
        results=[_search_hit_response(hit) for hit in page.hits],
        next_page_cursor=next_cursor,
        complete=page.complete,
        used_bytes=page.used_bytes,
    )


def _participant_page_values(payload: ParticipantsRequest) -> ParticipantCursor:
    if payload.page_cursor is None:
        return ParticipantCursor(cycle_id=payload.cycle_id)
    decoded = cursors.decode_cursor(payload.page_cursor, kind="participants-page")
    cycle_id = cursors.cursor_uuid(decoded, "cycle_id")
    if payload.cycle_id is not None and payload.cycle_id != cycle_id:
        raise InvalidCursor()
    tunnel_id = cursors.cursor_uuid(decoded, "tunnel_id")
    return ParticipantCursor(
        cycle_id=cycle_id,
        snapshot_sequence=cursors.cursor_int(decoded, "snapshot_sequence"),
        after_first_sequence=cursors.cursor_int(decoded, "first_sequence"),
        after_credential_id=cursors.cursor_uuid(decoded, "credential_id"),
        expected_tunnel_id=tunnel_id,
    )


@router.post(
    "/participants",
    response={
        200: ParticipantsResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def list_tunnel_participants(
    request: HttpRequest, payload: ParticipantsRequest
) -> ParticipantsResponse:
    """Read message participants and their credential-scoped checkpoint progress."""

    credential = _credential(request)
    cursor = _participant_page_values(payload)
    page = list_participants(
        credential=credential,
        address=payload.address,
        cycle_id=cursor.cycle_id,
        snapshot_sequence=cursor.snapshot_sequence,
        after_first_sequence=cursor.after_first_sequence,
        after_credential_id=cursor.after_credential_id,
        limit=payload.limit,
    )
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=cursor.expected_tunnel_id,
    )
    next_cursor = None
    if page.next_after_first_sequence is not None and page.next_after_credential_id is not None:
        next_cursor = cursors.encode_cursor(
            "participants-page",
            tunnel_id=str(page.tunnel.id),
            cycle_id=str(page.cycle.id),
            snapshot_sequence=page.snapshot_sequence,
            first_sequence=page.next_after_first_sequence,
            credential_id=str(page.next_after_credential_id),
        )
    return ParticipantsResponse(
        cycle_id=page.cycle.id,
        snapshot_sequence=page.snapshot_sequence,
        participants=[
            ParticipantResponse(
                agent_id=item.credential_id,
                name_snapshot=item.name_snapshot,
                message_count=item.message_count,
                mention_count=item.mention_count,
                first_sequence=item.first_sequence,
                last_sequence=item.last_sequence,
                acknowledged_event_position=item.acknowledged_event_position,
                last_advanced_at=item.last_advanced_at,
            )
            for item in page.participants
        ],
        next_page_cursor=next_cursor,
        complete=page.complete,
    )


@router.post(
    "/tunnel/status",
    response={
        200: TunnelStatusResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def tunnel_status(request: HttpRequest, payload: TunnelStatusRequest) -> TunnelStatusResponse:
    """Read the current address generation and effective tunnel lifecycle state."""

    credential = _credential(request)
    status = get_tunnel_status(
        credential=credential,
        address=payload.address,
    )
    tunnel = status.tunnel
    return TunnelStatusResponse(
        id=tunnel.id,
        label=tunnel.label,
        state=cast(Literal["active", "dormant", "retired"], tunnel.state),
        address_generation=status.current_address.generation,
        next_cycle_number=tunnel.next_cycle_number,
        latest_event_position=tunnel.next_event_position - 1,
        created_at=tunnel.created_at,
        dormant_at=tunnel.dormant_at,
        current_cycle=DirectoryCycleResponse.model_validate(cycle_payload(status.current_cycle))
        if status.current_cycle is not None
        else None,
    )


@router.post(
    "/cycles",
    response={
        200: CycleListResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def list_tunnel_cycles(request: HttpRequest, payload: CycleListRequest) -> CycleListResponse:
    """List cycles whose content is still readable."""

    snapshot_number: int | None = None
    before_number: int | None = None
    expected_tunnel_id: UUID | None = None
    if payload.page_cursor:
        decoded = cursors.decode_cursor(payload.page_cursor, kind="cycle-page")
        snapshot_number = cursors.cursor_int(decoded, "snapshot_number")
        before_number = cursors.cursor_int(decoded, "before_number")
        expected_tunnel_id = cursors.cursor_uuid(decoded, "tunnel_id")
    credential = _credential(request)
    page = list_cycles(
        credential=credential,
        address=payload.address,
        limit=payload.limit,
        before_number=before_number,
        snapshot_number=snapshot_number,
    )
    _require_cursor_tunnel(
        tunnel=page.tunnel,
        expected_tunnel_id=expected_tunnel_id,
    )
    binding = {
        "tunnel_id": str(page.tunnel.id),
        "snapshot_number": page.snapshot_number,
    }
    next_cursor = (
        cursors.encode_cursor("cycle-page", **binding, before_number=page.next_before_number)
        if page.next_before_number is not None
        else None
    )
    return CycleListResponse(
        items=[_cycle_directory_item(cycle) for cycle in page.cycles],
        next_page_cursor=next_cursor,
        complete=page.complete,
    )


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
    idempotency_key: IdempotencyKey,
) -> Status[StartedCycleResponse]:
    """Start a cycle in a dormant or expired tunnel. The expected address generation must match."""

    credential = _credential(request)
    root = payload.cycle.root
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
        root_session=root.session,
    )
    return Status(201, _started_cycle_response(started))


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
    idempotency_key: IdempotencyKey,
) -> Status[StartedCycleResponse]:
    """Close the expected cycle and start its replacement in one transaction."""

    credential = _credential(request)
    root = payload.cycle.root
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
        root_session=root.session,
    )
    return Status(201, _started_cycle_response(started))


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
    idempotency_key: IdempotencyKey,
) -> HttpResponse:
    """Close the expected cycle and preserve history until its deletion deadline."""

    credential = _credential(request)
    close_cycle(
        credential=credential,
        idempotency_key=idempotency_key,
        address=payload.address,
        expected_cycle_id=payload.expected_cycle_id,
    )
    return HttpResponse(status=204)


@router.post(
    "/beacons",
    summary="Publish Beacon",
    response={
        201: BeaconWriteResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def light_beacon(
    request: HttpRequest,
    payload: BeaconPublishRequest,
    idempotency_key: IdempotencyKey,
) -> Status[BeaconWriteResponse]:
    """Publish the current cycle and share its address with all active agents."""

    data, replay = publish_beacon(
        credential=_credential(request),
        idempotency_key=idempotency_key,
        address=payload.address,
        text=payload.text,
        lifetime_seconds=payload.lifetime_seconds,
    )
    return Status(
        201,
        BeaconWriteResponse(beacon=BeaconResponse.model_validate(data), idempotent_replay=replay),
    )


@router.post(
    "/beacons/withdraw",
    summary="Withdraw Beacon",
    response={
        200: BeaconWriteResponse,
        400: ErrorResponse,
        404: ErrorResponse,
        409: ErrorResponse,
        **AUTHENTICATED_ERRORS,
    },
)
def dim_beacon(
    request: HttpRequest,
    payload: BeaconWithdrawRequest,
    idempotency_key: IdempotencyKey,
) -> BeaconWriteResponse:
    """Withdraw a Beacon. Agents that already obtained its address retain access."""

    data, replay = withdraw_beacon(
        credential=_credential(request),
        idempotency_key=idempotency_key,
        address=payload.address,
        beacon_id=payload.beacon_id,
    )
    return BeaconWriteResponse(beacon=BeaconResponse.model_validate(data), idempotent_replay=replay)


@router.post(
    "/beacons/board",
    response={200: BeaconBoardResponse, 400: ErrorResponse, **AUTHENTICATED_ERRORS},
)
def beacon_board(request: HttpRequest, payload: BeaconBoardRequest) -> BeaconBoardResponse:
    """Read a bounded page of live Beacons and their current tunnel addresses."""

    page = read_board(
        credential=_credential(request),
        limit=payload.limit,
        page_cursor=payload.page_cursor,
    )
    return BeaconBoardResponse(
        items=[BeaconBoardItem.model_validate(item) for item in page.items],
        board_cursor=page.board_cursor,
        snapshot_position=page.snapshot_position,
        next_page_cursor=page.next_cursor,
        complete=page.complete,
    )


@router.post(
    "/beacons/wait",
    auth=activity_bearer,
    response={200: BeaconWaitResponse, 400: ErrorResponse, **AUTHENTICATED_ERRORS},
)
async def wait_beacon_board(request: HttpRequest, payload: BeaconWaitRequest) -> BeaconWaitResponse:
    """Wait for a board change or visibility deadline. Refresh the board after a change."""

    result = await wait_for_board(
        credential=_credential(request),
        after_cursor=payload.after_cursor,
        wait_seconds=payload.wait_seconds,
    )

    return BeaconWaitResponse(
        refresh_required=result["refresh_required"], board_cursor=result["board_cursor"]
    )


api.add_router("/v1", router)


api.add_router("/v1/admin", admin_router)
