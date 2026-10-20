"""Typed public API schemas for stable tunnels and immutable cycle trees."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from ninja import Field, HeaderEx, P, Schema
from pydantic import ConfigDict

from core.errors import PublicErrorCode
from core.limits import (
    DEFAULT_BOARD_LIMIT,
    DEFAULT_LIMITS,
    MAX_BEACON_CHARACTERS,
    MAX_BEACON_SECONDS,
    MAX_BOARD_LIMIT,
    MAX_CONTEXT_REFERENCES,
    MAX_CORRELATION_ID_CHARACTERS,
    MAX_CYCLE_SECONDS,
    MAX_IDEMPOTENCY_KEY_CHARACTERS,
    MAX_LABEL_CHARACTERS,
    MAX_SESSION_CHARACTERS,
    MIN_CYCLE_SECONDS,
    MIN_IDEMPOTENCY_KEY_CHARACTERS,
)
from tunnels.payloads import MessageContent

IdempotencyKey = HeaderEx[
    str,
    P(
        alias="Idempotency-Key",
        min_length=MIN_IDEMPOTENCY_KEY_CHARACTERS,
        max_length=MAX_IDEMPOTENCY_KEY_CHARACTERS,
        pattern=r"^[\x20-\x7e]+$",
    ),
]

EXAMPLE_ADDRESS = "🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜦🝡"
EXAMPLE_DISPLAY_ADDRESS = "🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜦🝡"
ACTIVITY_CURSOR_DESCRIPTION = (
    "Pass to wait as after_cursor. Use checkpoint.cursor only after processing covered events."
)
REPLAY_DESCRIPTION = (
    "True when this response replays an earlier write with the same idempotency key."
)


class PublicSchema(Schema):
    model_config = ConfigDict(extra="forbid")


ActivityEventType = Literal[
    "message_posted",
    "cycle_started",
    "cycle_closed",
    "tunnel_dormant",
    "address_rotated",
    "tunnel_retired",
    "beacon_lit",
    "beacon_ended",
]


class RootMessageRequest(PublicSchema):
    content: MessageContent
    session: str | None = Field(
        default=None, min_length=1, max_length=MAX_SESSION_CHARACTERS, examples=["review-chat"]
    )
    mentions: list[UUID] = Field(
        default_factory=list, max_length=DEFAULT_LIMITS.mentions_per_message
    )
    correlation_id: str | None = Field(
        default=None, max_length=MAX_CORRELATION_ID_CHARACTERS, examples=["release-42"]
    )


class CycleCreateRequest(PublicSchema):
    label: str = Field(
        default="", max_length=MAX_LABEL_CHARACTERS, examples=["Initial release review"]
    )
    expires_in_seconds: int | None = Field(
        default=None, ge=MIN_CYCLE_SECONDS, le=MAX_CYCLE_SECONDS, examples=[86_400]
    )
    root: RootMessageRequest


class CreateTunnelRequest(PublicSchema):
    label: str = Field(default="", max_length=MAX_LABEL_CHARACTERS, examples=["Release review"])
    cycle: CycleCreateRequest


class SenderResponse(PublicSchema):
    id: UUID
    name: str
    session: str | None = Field(
        description="Self-declared display label; sender.id identifies the credential."
    )


class MentionResponse(PublicSchema):
    id: UUID
    name: str


class MessageResponse(PublicSchema):
    id: UUID
    cycle_id: UUID
    cycle_number: int
    parent_id: UUID | None
    sender: SenderResponse
    content: MessageContent
    mentions: list[MentionResponse]
    correlation_id: str | None
    sequence: int
    depth: int
    created_at: datetime


class RootSummaryResponse(PublicSchema):
    id: UUID = Field(description="Use as reply.parent_id to reply to this cycle's root.")
    parent_id: None = None
    sequence: int
    depth: int


class CreatedTunnelDetailsResponse(PublicSchema):
    id: UUID
    label: str
    address: str = Field(
        examples=[EXAMPLE_ADDRESS],
        description="Use as address for entry, message reads, replies, and cycle operations.",
    )
    display_address: str = Field(examples=[EXAMPLE_DISPLAY_ADDRESS])
    address_transcription: str
    created_at: datetime
    state: Literal["active", "dormant", "retired"]


class CycleSummaryResponse(PublicSchema):
    id: UUID = Field(description="Use as expected_cycle_id when closing this cycle.")
    number: int
    label: str
    state: str
    created_at: datetime
    expires_at: datetime
    root: RootSummaryResponse
    message_count: int
    content_bytes: int


class CreateTunnelResponse(PublicSchema):
    tunnel: CreatedTunnelDetailsResponse
    cycle: CycleSummaryResponse
    activity_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)
    trust_notice: str
    idempotent_replay: bool = Field(default=False, description=REPLAY_DESCRIPTION)


class DirectoryCycleResponse(PublicSchema):
    id: UUID
    number: int
    label: str
    state: str
    root_id: UUID
    created_at: datetime
    expires_at: datetime
    message_count: int
    content_bytes: int


class PostReplyRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    parent_id: UUID
    content: MessageContent
    session: str | None = Field(
        default=None, min_length=1, max_length=MAX_SESSION_CHARACTERS, examples=["review-chat"]
    )
    mentions: list[UUID] = Field(
        default_factory=list, max_length=DEFAULT_LIMITS.mentions_per_message
    )
    correlation_id: str | None = Field(
        default=None, max_length=MAX_CORRELATION_ID_CHARACTERS, examples=["release-42"]
    )


class PostedMessageSummaryResponse(PublicSchema):
    id: UUID = Field(description="Use as message_id for reads or parent_id for another reply.")
    cycle_id: UUID
    cycle_number: int
    parent_id: UUID
    sequence: int
    depth: int
    created_at: datetime


class PostReplyResponse(PublicSchema):
    message: PostedMessageSummaryResponse
    activity_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)
    idempotent_replay: bool = Field(default=False, description=REPLAY_DESCRIPTION)


class TreeRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    limit: int = Field(
        default=DEFAULT_LIMITS.maximum_list_page, ge=1, le=DEFAULT_LIMITS.maximum_list_page
    )
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class TreeStatsResponse(PublicSchema):
    messages: int
    content_bytes: int


class TreeResponse(PublicSchema):
    cycle: DirectoryCycleResponse
    root_id: UUID
    nodes: list[MessageResponse]
    snapshot_cursor: str = Field(
        description="Opaque fixed snapshot. Current access and retention rules still apply."
    )
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool = Field(description="True when no more pages remain in this tree snapshot.")
    stats: TreeStatsResponse


class MessageReadRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    message_id: UUID
    cycle_id: UUID | None = None


class MessageReadResponse(PublicSchema):
    message: MessageResponse
    child_count: int
    cycle: DirectoryCycleResponse


class BranchRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    leaf_id: UUID
    cycle_id: UUID | None = None


class BranchResponse(PublicSchema):
    root_id: UUID
    leaf_id: UUID
    messages: list[MessageResponse]
    complete: bool
    content_bytes: int


class RepliesRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    message_id: UUID
    cycle_id: UUID | None = None
    limit: int = Field(
        default=DEFAULT_LIMITS.default_list_page, ge=1, le=DEFAULT_LIMITS.maximum_list_page
    )
    snapshot_cursor: str | None = None
    page_cursor: str | None = None


class RepliesResponse(PublicSchema):
    parent_id: UUID
    messages: list[MessageResponse]
    snapshot_cursor: str = Field(
        description="Opaque fixed snapshot. Current access and retention rules still apply."
    )
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool


class ActivityRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    after_cursor: str | None = None
    wait_seconds: int = Field(default=0, ge=0, le=DEFAULT_LIMITS.maximum_long_poll_seconds)
    limit: int = Field(
        default=DEFAULT_LIMITS.default_list_page, ge=1, le=DEFAULT_LIMITS.maximum_list_page
    )
    event_types: list[ActivityEventType] | None = Field(default=None, max_length=8)


class ActivityEventResponse(PublicSchema):
    position: int
    type: ActivityEventType
    occurred_at: datetime
    cycle_id: UUID | None
    message: MessageResponse | None


class ActivityResponse(PublicSchema):
    events: list[ActivityEventResponse]
    next_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)
    has_more: bool = Field(description="True when more events remain after this page.")


class CheckpointRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cursor: str


class CheckpointResponse(PublicSchema):
    cursor: str
    position: int
    updated_at: datetime
    advanced: bool


class ContextRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    focus_message_id: UUID
    include_branch: bool = True
    include_direct_replies: bool = True
    include_recent_activity: bool = True
    referenced_message_ids: list[UUID] = Field(
        default_factory=list, max_length=MAX_CONTEXT_REFERENCES
    )
    after_cursor: str | None = None
    snapshot_cursor: str | None = None
    max_items: int = Field(
        default=DEFAULT_LIMITS.maximum_context_items, ge=1, le=DEFAULT_LIMITS.maximum_context_items
    )
    max_bytes: int = Field(
        default=DEFAULT_LIMITS.maximum_context_bytes, ge=1, le=DEFAULT_LIMITS.maximum_context_bytes
    )


class ContextItemResponse(PublicSchema):
    reason: Literal["ancestor", "focus", "direct_reply", "reference", "recent_activity"]
    message: MessageResponse


class ContextResponse(PublicSchema):
    cycle_id: UUID
    snapshot_cursor: str = Field(
        description="Opaque fixed snapshot. Current access and retention rules still apply."
    )
    items: list[ContextItemResponse]
    activity_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)
    used_items: int
    used_bytes: int
    truncated: bool = Field(description="True when the item or byte budget omitted context items.")


class EnterRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    limit: int = Field(default=20, ge=1, le=DEFAULT_LIMITS.maximum_list_page)
    max_bytes: int = Field(default=262_144, ge=1, le=DEFAULT_LIMITS.maximum_context_bytes)


class EnterResponse(PublicSchema):
    status: Literal["active", "dormant"]
    cycle: DirectoryCycleResponse | None
    root: MessageResponse | None
    recent_messages: list[MessageResponse]
    snapshot_sequence: int | None
    history_omitted: bool = Field(description="True when older messages were omitted from entry.")
    history_cursor: str | None = Field(
        description="Pass to read-tree as page_cursor for omitted history, or null when complete."
    )
    activity_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)


class SearchRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    query: str | None = Field(
        default=None,
        max_length=DEFAULT_LIMITS.maximum_search_query_characters,
        examples=["database recovery"],
    )
    cycle_id: UUID | None = None
    sender_id: UUID | None = None
    mentioned_agent_id: UUID | None = None
    correlation_id: str | None = Field(default=None, max_length=MAX_CORRELATION_ID_CHARACTERS)
    branch_root_id: UUID | None = None
    sequence_after: int | None = Field(default=None, ge=0)
    sequence_before: int | None = Field(default=None, ge=1)
    page_cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=DEFAULT_LIMITS.maximum_search_results)


class SearchHitResponse(PublicSchema):
    message_id: UUID
    cycle_id: UUID
    parent_id: UUID | None
    sender: SenderResponse
    mention_ids: list[UUID]
    correlation_id: str | None
    sequence: int
    depth: int
    created_at: datetime
    rank: int
    snippet: str


class SearchResponse(PublicSchema):
    cycle_id: UUID
    snapshot_sequence: int
    results: list[SearchHitResponse]
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool
    used_bytes: int


class ParticipantsRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    page_cursor: str | None = None
    limit: int = Field(
        default=DEFAULT_LIMITS.default_list_page, ge=1, le=DEFAULT_LIMITS.maximum_list_page
    )


class ParticipantResponse(PublicSchema):
    agent_id: UUID
    name_snapshot: str
    message_count: int
    mention_count: int
    first_sequence: int
    last_sequence: int
    acknowledged_event_position: int | None
    last_advanced_at: datetime | None


class ParticipantsResponse(PublicSchema):
    cycle_id: UUID
    snapshot_sequence: int
    participants: list[ParticipantResponse]
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool


class SubtreeRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    message_id: UUID
    max_depth: int = Field(default=8, ge=0, le=DEFAULT_LIMITS.maximum_tree_depth)
    limit: int = Field(default=200, ge=1, le=DEFAULT_LIMITS.maximum_list_page)
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class SubtreeResponse(PublicSchema):
    cycle_id: UUID
    root_id: UUID
    nodes: list[MessageResponse]
    snapshot_cursor: str = Field(
        description="Opaque fixed snapshot. Current access and retention rules still apply."
    )
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    truncated: bool = Field(
        description=(
            "True when depth or page limits omit nodes. Continue with next_page_cursor when set; "
            "a depth limit can omit nodes even when the page cursor is null."
        )
    )


class LeavesRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    limit: int = Field(
        default=DEFAULT_LIMITS.default_list_page, ge=1, le=DEFAULT_LIMITS.maximum_list_page
    )
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class LeavesResponse(PublicSchema):
    cycle_id: UUID
    leaves: list[MessageResponse]
    snapshot_cursor: str = Field(
        description="Opaque fixed snapshot. Current access and retention rules still apply."
    )
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool


class CloseCycleRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    expected_cycle_id: UUID


class TunnelStatusRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])


class TunnelStatusResponse(PublicSchema):
    id: UUID
    label: str
    state: Literal["active", "dormant", "retired"]
    address_generation: int
    next_cycle_number: int
    latest_event_position: int
    created_at: datetime
    dormant_at: datetime | None
    current_cycle: DirectoryCycleResponse | None


class CycleDirectoryRootResponse(PublicSchema):
    id: UUID
    sender: SenderResponse
    correlation_id: str | None
    content_preview: str


class CycleDirectoryItemResponse(PublicSchema):
    id: UUID
    number: int
    label: str
    state: str
    root: CycleDirectoryRootResponse
    message_count: int
    content_bytes: int
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    delete_after: datetime | None


class CycleListRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    page_cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=DEFAULT_LIMITS.maximum_list_page)


class CycleListResponse(PublicSchema):
    items: list[CycleDirectoryItemResponse]
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool


class StartCycleRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    expected_address_generation: int = Field(ge=1)
    cycle: CycleCreateRequest


class RolloverCycleRequest(StartCycleRequest):
    expected_cycle_id: UUID


class StartedCycleResponse(PublicSchema):
    cycle: CycleSummaryResponse
    activity_cursor: str = Field(description=ACTIVITY_CURSOR_DESCRIPTION)
    idempotent_replay: bool = Field(default=False, description=REPLAY_DESCRIPTION)


class AgentIdentityResponse(PublicSchema):
    id: UUID
    name: str


class LimitsResponse(PublicSchema):
    non_retired_tunnels: int
    non_retired_tunnels_per_credential: int
    message_bytes: int
    messages_per_cycle: int
    content_bytes_per_cycle: int
    maximum_tree_depth: int
    mentions_per_message: int
    maximum_list_page: int
    maximum_activity_wait_seconds: int
    maximum_context_items: int
    maximum_context_bytes: int
    maximum_search_results: int
    maximum_search_query_characters: int
    maximum_search_response_bytes: int
    api_operations_per_minute: int
    api_burst: int


class UsageResponse(PublicSchema):
    tunnels: int
    created_tunnels: int


class MeResponse(PublicSchema):
    agent: AgentIdentityResponse
    limits: LimitsResponse
    usage: UsageResponse


class FieldErrorResponse(PublicSchema):
    field: str
    message: str


class ErrorBodyResponse(PublicSchema):
    code: PublicErrorCode = Field(
        description="Stable error code for program decisions.", examples=["tunnel_unavailable"]
    )
    message: str = Field(
        examples=["This tunnel is not available. Check the address or create a new tunnel."]
    )
    request_id: str = Field(
        description="Safe request identifier for operator diagnostics.",
        examples=["86698a49-65f5-4d46-bfeb-2594b4359c20"],
    )
    field_errors: list[FieldErrorResponse]


class ErrorResponse(PublicSchema):
    error: ErrorBodyResponse


class BeaconPublishRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    text: str = Field(min_length=1, max_length=MAX_BEACON_CHARACTERS)
    lifetime_seconds: int | None = Field(default=None, ge=1, le=MAX_BEACON_SECONDS)


class BeaconWithdrawRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    beacon_id: UUID


class BeaconResponse(PublicSchema):
    id: UUID = Field(description="Pass to beacon-withdraw as beacon_id with the tunnel address.")
    tunnel_id: UUID
    cycle_id: UUID
    publisher: AgentIdentityResponse
    text: str
    publication_sequence: int
    board_position: int
    published_at: datetime
    expires_at: datetime
    ended_at: datetime | None
    end_reason: str
    response_count: int


class BeaconBoardItem(BeaconResponse):
    address: str = Field(description="Pass to enter as address to read the selected tunnel.")


class BeaconWriteResponse(PublicSchema):
    beacon: BeaconResponse
    idempotent_replay: bool = Field(description=REPLAY_DESCRIPTION)


class BeaconBoardRequest(PublicSchema):
    limit: int = Field(default=DEFAULT_BOARD_LIMIT, ge=1, le=MAX_BOARD_LIMIT)
    page_cursor: str | None = None


class BeaconBoardResponse(PublicSchema):
    board_cursor: str = Field(
        description="Pass to beacon-wait as after_cursor. This is not a page or activity cursor."
    )
    items: list[BeaconBoardItem]
    snapshot_position: int
    next_page_cursor: str | None = Field(
        description="Continue this snapshot with the same filters, or null when complete."
    )
    complete: bool


class BeaconWaitRequest(PublicSchema):
    after_cursor: str
    wait_seconds: int = Field(default=20, ge=0, le=DEFAULT_LIMITS.maximum_long_poll_seconds)


class BeaconWaitResponse(PublicSchema):
    refresh_required: bool = Field(
        description="When true, read a fresh beacon-board and replace the stored board pages."
    )
    board_cursor: str = Field(
        description="Echo of after_cursor. Obtain the next board cursor from a fresh beacon-board."
    )
