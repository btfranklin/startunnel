"""Typed public API schemas for stable tunnels and immutable cycle trees."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from ninja import Field, Schema
from pydantic import ConfigDict

EXAMPLE_ADDRESS = "🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜣🜦🝡"
EXAMPLE_DISPLAY_ADDRESS = "🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜣🜣 🜣🜣🜦🝡"


class PublicSchema(Schema):
    model_config = ConfigDict(extra="forbid")


class TextContent(PublicSchema):
    type: Literal["text"]
    text: str = Field(examples=["Ready for review."])


class JsonContent(PublicSchema):
    type: Literal["json"]
    value: Any = Field(examples=[{"task": "review", "artifact": "report-42"}])


MessageContent = Annotated[TextContent | JsonContent, Field(discriminator="type")]
ActivityEventType = Literal[
    "message_posted",
    "cycle_started",
    "cycle_closed",
    "tunnel_dormant",
    "address_rotated",
    "tunnel_retired",
]


class RootMessageRequest(PublicSchema):
    content: MessageContent
    mentions: list[UUID] = Field(default_factory=list, max_length=32)
    correlation_id: str | None = Field(default=None, max_length=128, examples=["release-42"])


class CycleCreateRequest(PublicSchema):
    label: str = Field(default="", max_length=160, examples=["Initial release review"])
    expires_in_seconds: int | None = Field(default=None, ge=300, le=604_800, examples=[86_400])
    root: RootMessageRequest


class CreateTunnelRequest(PublicSchema):
    label: str = Field(default="", max_length=160, examples=["Release review"])
    cycle: CycleCreateRequest


class SenderResponse(PublicSchema):
    id: UUID
    name: str


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
    id: UUID
    parent_id: None = None
    sequence: int
    depth: int


class CreatedTunnelDetailsResponse(PublicSchema):
    id: UUID
    label: str
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    display_address: str = Field(examples=[EXAMPLE_DISPLAY_ADDRESS])
    address_transcription: str
    created_at: datetime
    state: Literal["active", "dormant", "retired"]


class CycleSummaryResponse(PublicSchema):
    id: UUID
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
    activity_cursor: str
    trust_notice: str
    idempotent_replay: bool = False


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


class TunnelMetadataResponse(PublicSchema):
    id: UUID
    label: str
    state: Literal["active", "dormant", "retired"]
    created_at: datetime
    address: str | None = None
    display_address: str | None = None
    address_transcription: str | None = None
    current_cycle: DirectoryCycleResponse | None


class PostReplyRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    parent_id: UUID
    content: MessageContent
    mentions: list[UUID] = Field(default_factory=list, max_length=32)
    correlation_id: str | None = Field(default=None, max_length=128, examples=["release-42"])


class PostedMessageSummaryResponse(PublicSchema):
    id: UUID
    cycle_id: UUID
    cycle_number: int
    parent_id: UUID
    sequence: int
    depth: int
    created_at: datetime


class PostReplyResponse(PublicSchema):
    message: PostedMessageSummaryResponse
    activity_cursor: str
    idempotent_replay: bool = False


class TreeRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    limit: int = Field(default=1000, ge=1, le=1000)
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class TreeStatsResponse(PublicSchema):
    messages: int
    content_bytes: int


class TreeResponse(PublicSchema):
    cycle: DirectoryCycleResponse
    root_id: UUID
    nodes: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool
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
    limit: int = Field(default=100, ge=1, le=1000)
    snapshot_cursor: str | None = None
    page_cursor: str | None = None


class RepliesResponse(PublicSchema):
    parent_id: UUID
    messages: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool


class ActivityRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    after_cursor: str | None = None
    wait_seconds: int = Field(default=0, ge=0, le=20)
    limit: int = Field(default=100, ge=1, le=1000)
    event_types: list[ActivityEventType] | None = Field(default=None, max_length=6)


class ActivityEventResponse(PublicSchema):
    position: int
    type: ActivityEventType
    occurred_at: datetime
    cycle_id: UUID | None
    message: MessageResponse | None


class ActivityResponse(PublicSchema):
    events: list[ActivityEventResponse]
    next_cursor: str
    has_more: bool


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
    referenced_message_ids: list[UUID] = Field(default_factory=list, max_length=100)
    after_cursor: str | None = None
    snapshot_cursor: str | None = None
    max_items: int = Field(default=200, ge=1, le=200)
    max_bytes: int = Field(default=1_048_576, ge=1, le=1_048_576)


class ContextItemResponse(PublicSchema):
    reason: Literal["ancestor", "focus", "direct_reply", "reference", "recent_activity"]
    message: MessageResponse


class ContextResponse(PublicSchema):
    cycle_id: UUID
    snapshot_cursor: str
    items: list[ContextItemResponse]
    activity_cursor: str
    used_items: int
    used_bytes: int
    truncated: bool


class SearchRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    query: str | None = Field(default=None, max_length=512, examples=["database recovery"])
    cycle_id: UUID | None = None
    sender_id: UUID | None = None
    mentioned_agent_id: UUID | None = None
    correlation_id: str | None = Field(default=None, max_length=128)
    branch_root_id: UUID | None = None
    sequence_after: int | None = Field(default=None, ge=0)
    sequence_before: int | None = Field(default=None, ge=1)
    page_cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


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
    next_page_cursor: str | None
    complete: bool
    used_bytes: int


class ParticipantsRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    page_cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=1000)


class ParticipantResponse(PublicSchema):
    agent_id: UUID
    name_snapshot: str
    message_count: int
    mention_count: int
    first_sequence: int
    last_sequence: int


class ParticipantsResponse(PublicSchema):
    cycle_id: UUID
    snapshot_sequence: int
    participants: list[ParticipantResponse]
    next_page_cursor: str | None
    complete: bool


class SubtreeRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    message_id: UUID
    max_depth: int = Field(default=8, ge=0, le=128)
    limit: int = Field(default=200, ge=1, le=1000)
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class SubtreeResponse(PublicSchema):
    cycle_id: UUID
    root_id: UUID
    nodes: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    truncated: bool


class LeavesRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    cycle_id: UUID | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    page_cursor: str | None = None
    snapshot_cursor: str | None = None


class LeavesResponse(PublicSchema):
    cycle_id: UUID
    leaves: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
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
    limit: int = Field(default=50, ge=1, le=1000)


class CycleListResponse(PublicSchema):
    items: list[CycleDirectoryItemResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool


class StartCycleRequest(PublicSchema):
    address: str = Field(examples=[EXAMPLE_ADDRESS])
    expected_address_generation: int = Field(ge=1)
    cycle: CycleCreateRequest


class RolloverCycleRequest(StartCycleRequest):
    expected_cycle_id: UUID


class StartedCycleResponse(PublicSchema):
    cycle: CycleSummaryResponse
    activity_cursor: str
    idempotent_replay: bool = False


class AgentIdentityResponse(PublicSchema):
    id: UUID
    name: str


class LimitsResponse(PublicSchema):
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


class MeResponse(PublicSchema):
    agent: AgentIdentityResponse
    limits: LimitsResponse
    usage: UsageResponse


class FieldErrorResponse(PublicSchema):
    field: str
    message: str


class ErrorBodyResponse(PublicSchema):
    code: str = Field(examples=["tunnel_unavailable"])
    message: str = Field(
        examples=["This tunnel is not available. Check the address or create a new tunnel."]
    )
    request_id: str = Field(examples=["86698a49-65f5-4d46-bfeb-2594b4359c20"])
    field_errors: list[FieldErrorResponse]


class ErrorResponse(PublicSchema):
    error: ErrorBodyResponse
