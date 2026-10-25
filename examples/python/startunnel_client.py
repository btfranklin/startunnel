"""A small asynchronous reference client for the StarTunnel public API.

This module is an example, not a separately released SDK. It keeps glyph
addresses in request bodies. It never logs request bodies or credentials.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict, cast

import httpx
from pydantic import TypeAdapter, ValidationError

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]


def json_value(value: object) -> JsonValue:
    """Check all members of an external JSON value."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        members: Sequence[object] = value
        return [json_value(member) for member in members]
    if isinstance(value, dict):
        entries: Mapping[object, object] = value
        result: JsonObject = {}
        for key, member in entries.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings.")
            result[key] = json_value(member)
        return result
    raise ValueError("Expected a JSON value.")


def json_object(value: object) -> JsonObject:
    checked = json_value(value)
    if not isinstance(checked, dict):
        raise ValueError("Expected a JSON object.")
    return checked


# Response records use JSON strings for UUID and date-time fields.
class TextContent(TypedDict):
    type: Literal["text"]
    text: str


class JsonContent(TypedDict):
    type: Literal["json"]
    value: JsonValue


type MessageContent = TextContent | JsonContent

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


class SenderResponse(TypedDict):
    id: str
    name: str
    session: str | None


class MentionResponse(TypedDict):
    id: str
    name: str


class MessageResponse(TypedDict):
    id: str
    cycle_id: str
    cycle_number: int
    parent_id: str | None
    sender: SenderResponse
    content: MessageContent
    mentions: list[MentionResponse]
    correlation_id: str | None
    sequence: int
    depth: int
    created_at: str


class RootSummaryResponse(TypedDict):
    id: str
    parent_id: None
    sequence: int
    depth: int


class CreatedTunnelDetailsResponse(TypedDict):
    id: str
    label: str
    address: str
    display_address: str
    address_transcription: str
    created_at: str
    state: Literal["active", "dormant", "retired"]


class CycleSummaryResponse(TypedDict):
    id: str
    number: int
    label: str
    state: str
    created_at: str
    expires_at: str
    root: RootSummaryResponse
    message_count: int
    content_bytes: int


class CreateTunnelResponse(TypedDict):
    tunnel: CreatedTunnelDetailsResponse
    cycle: CycleSummaryResponse
    activity_cursor: str
    trust_notice: str
    idempotent_replay: bool


class DirectoryCycleResponse(TypedDict):
    id: str
    number: int
    label: str
    state: str
    root_id: str
    created_at: str
    expires_at: str
    message_count: int
    content_bytes: int


class PostedMessageSummaryResponse(TypedDict):
    id: str
    cycle_id: str
    cycle_number: int
    parent_id: str
    sequence: int
    depth: int
    created_at: str


class PostReplyResponse(TypedDict):
    message: PostedMessageSummaryResponse
    activity_cursor: str
    idempotent_replay: bool


class TreeStatsResponse(TypedDict):
    messages: int
    content_bytes: int


class TreeResponse(TypedDict):
    cycle: DirectoryCycleResponse
    root_id: str
    nodes: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool
    stats: TreeStatsResponse


class MessageReadResponse(TypedDict):
    message: MessageResponse
    child_count: int
    cycle: DirectoryCycleResponse


class BranchResponse(TypedDict):
    root_id: str
    leaf_id: str
    messages: list[MessageResponse]
    complete: bool
    content_bytes: int


class RepliesResponse(TypedDict):
    parent_id: str
    messages: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool


class ActivityEventResponse(TypedDict):
    position: int
    type: ActivityEventType
    occurred_at: str
    cycle_id: str | None
    message: MessageResponse | None


class ActivityResponse(TypedDict):
    events: list[ActivityEventResponse]
    next_cursor: str
    has_more: bool


class CheckpointResponse(TypedDict):
    cursor: str
    position: int
    updated_at: str
    advanced: bool


class ContextItemResponse(TypedDict):
    reason: Literal["ancestor", "focus", "direct_reply", "reference", "recent_activity"]
    message: MessageResponse


class ContextResponse(TypedDict):
    cycle_id: str
    snapshot_cursor: str
    items: list[ContextItemResponse]
    activity_cursor: str
    used_items: int
    used_bytes: int
    truncated: bool


class SearchHitResponse(TypedDict):
    message_id: str
    cycle_id: str
    parent_id: str | None
    sender: SenderResponse
    mention_ids: list[str]
    correlation_id: str | None
    sequence: int
    depth: int
    created_at: str
    rank: int
    snippet: str


class SearchResponse(TypedDict):
    cycle_id: str
    snapshot_sequence: int
    results: list[SearchHitResponse]
    next_page_cursor: str | None
    complete: bool
    used_bytes: int


class ParticipantResponse(TypedDict):
    agent_id: str
    name_snapshot: str
    message_count: int
    mention_count: int
    first_sequence: int
    last_sequence: int
    acknowledged_event_position: int | None
    last_advanced_at: str | None


class ParticipantsResponse(TypedDict):
    cycle_id: str
    snapshot_sequence: int
    participants: list[ParticipantResponse]
    next_page_cursor: str | None
    complete: bool


class SubtreeResponse(TypedDict):
    cycle_id: str
    root_id: str
    nodes: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    truncated: bool


class LeavesResponse(TypedDict):
    cycle_id: str
    leaves: list[MessageResponse]
    snapshot_cursor: str
    next_page_cursor: str | None
    complete: bool


class TunnelStatusResponse(TypedDict):
    id: str
    label: str
    state: Literal["active", "dormant", "retired"]
    address_generation: int
    next_cycle_number: int
    latest_event_position: int
    created_at: str
    dormant_at: str | None
    current_cycle: DirectoryCycleResponse | None


class CycleDirectoryRootResponse(TypedDict):
    id: str
    sender: SenderResponse
    correlation_id: str | None
    content_preview: str


class CycleDirectoryItemResponse(TypedDict):
    id: str
    number: int
    label: str
    state: str
    root: CycleDirectoryRootResponse
    message_count: int
    content_bytes: int
    created_at: str
    expires_at: str
    closed_at: str | None
    delete_after: str | None


class CycleListResponse(TypedDict):
    items: list[CycleDirectoryItemResponse]
    next_page_cursor: str | None
    complete: bool


class StartedCycleResponse(TypedDict):
    cycle: CycleSummaryResponse
    activity_cursor: str
    idempotent_replay: bool


class AgentIdentityResponse(TypedDict):
    id: str
    name: str


class LimitsResponse(TypedDict):
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


class UsageResponse(TypedDict):
    tunnels: int
    created_tunnels: int


class MeResponse(TypedDict):
    agent: AgentIdentityResponse
    limits: LimitsResponse
    usage: UsageResponse


class EnterResponse(TypedDict):
    status: Literal["active", "dormant"]
    cycle: DirectoryCycleResponse | None
    root: MessageResponse | None
    recent_messages: list[MessageResponse]
    snapshot_sequence: int | None
    history_omitted: bool
    history_cursor: str | None
    activity_cursor: str


class BeaconResponse(TypedDict):
    id: str
    tunnel_id: str
    cycle_id: str
    publisher: AgentIdentityResponse
    text: str
    publication_sequence: int
    board_position: int
    published_at: str
    expires_at: str
    ended_at: str | None
    end_reason: str
    response_count: int


class BeaconBoardItem(BeaconResponse):
    address: str


class BeaconWriteResponse(TypedDict):
    beacon: BeaconResponse
    idempotent_replay: bool


class BeaconBoardResponse(TypedDict):
    board_cursor: str
    items: list[BeaconBoardItem]
    snapshot_position: int
    next_page_cursor: str | None
    complete: bool


class BeaconWaitResponse(TypedDict):
    refresh_required: bool
    board_cursor: str


_ACTIVITYRESPONSE = TypeAdapter(ActivityResponse)
_BRANCHRESPONSE = TypeAdapter(BranchResponse)
_CHECKPOINTRESPONSE = TypeAdapter(CheckpointResponse)
_CONTEXTRESPONSE = TypeAdapter(ContextResponse)
_CREATETUNNELRESPONSE = TypeAdapter(CreateTunnelResponse)
_CYCLELISTRESPONSE = TypeAdapter(CycleListResponse)
_LEAVESRESPONSE = TypeAdapter(LeavesResponse)
_MERESPONSE = TypeAdapter(MeResponse)
_MESSAGEREADRESPONSE = TypeAdapter(MessageReadResponse)
_PARTICIPANTSRESPONSE = TypeAdapter(ParticipantsResponse)
_POSTREPLYRESPONSE = TypeAdapter(PostReplyResponse)
_REPLIESRESPONSE = TypeAdapter(RepliesResponse)
_SEARCHRESPONSE = TypeAdapter(SearchResponse)
_STARTEDCYCLERESPONSE = TypeAdapter(StartedCycleResponse)
_SUBTREERESPONSE = TypeAdapter(SubtreeResponse)
_TREERESPONSE = TypeAdapter(TreeResponse)
_TUNNELSTATUSRESPONSE = TypeAdapter(TunnelStatusResponse)


class StarTunnelError(RuntimeError):
    """A safe API error that does not include a key, address, or message."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str | None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        suffix = f" Request ID: {request_id}." if request_id else ""
        super().__init__(f"StarTunnel returned {status_code} ({code}). {message}{suffix}")


def load_env_file(path: str | Path = ".env.tutorial", *, names: set[str] | None = None) -> None:
    """Load selected KEY=VALUE entries without replacing environment values."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if names is not None and name not in names:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if name:
            os.environ.setdefault(name, value)


def require_environment(*names: str) -> dict[str, str]:
    """Return required environment values without printing their contents."""

    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(
            f"Missing required environment setting(s): {joined}. "
            "Add them to .env.tutorial, then run the command again."
        )
    return {name: os.environ[name] for name in names}


def new_idempotency_key() -> str:
    """Create a retry key for one logical write operation."""

    return f"example-{uuid.uuid4()}"


class StarTunnelClient:
    """Typed convenience methods for the StarTunnel JSON API."""

    def __init__(self, base_url: str, agent_key: str) -> None:
        if not base_url:
            raise ValueError("STARTUNNEL_BASE_URL is empty.")
        if not agent_key:
            raise ValueError("The StarTunnel agent key is empty.")

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {agent_key}",
                "Accept": "application/json",
                "User-Agent": "startunnel-python-example",
            },
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
        )

    async def __aenter__(self) -> StarTunnelClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""

        await self._client.aclose()

    async def me(self) -> MeResponse:
        """Return the current agent identity, usage, and limits."""

        return await self._json_request(
            "GET", "/api/v1/me", response_type=_MERESPONSE, expected={200}
        )

    async def create_tunnel(
        self,
        *,
        label: str,
        root_content: JsonObject,
        idempotency_key: str,
        cycle_label: str = "",
        expires_in_seconds: int | None = None,
        root_mentions: list[str] | None = None,
        root_correlation_id: str | None = None,
        root_session: str | None = None,
    ) -> CreateTunnelResponse:
        """Create a stable tunnel and its first immutable root message."""

        body = self._create_tunnel_body(
            label=label,
            root_content=root_content,
            cycle_label=cycle_label,
            expires_in_seconds=expires_in_seconds,
            root_mentions=root_mentions,
            root_correlation_id=root_correlation_id,
            root_session=root_session,
        )
        return await self._json_request(
            "POST",
            "/api/v1/tunnels",
            json=body,
            idempotency_key=idempotency_key,
            response_type=_CREATETUNNELRESPONSE,
            expected={200, 201},
        )

    async def post_reply(
        self,
        *,
        address: str,
        parent_id: str,
        content: JsonObject,
        idempotency_key: str,
        mentions: list[str] | None = None,
        correlation_id: str | None = None,
        session: str | None = None,
    ) -> PostReplyResponse:
        """Add one immutable reply below a required parent message."""

        return await self._json_request(
            "POST",
            "/api/v1/messages",
            json=self._reply_body(
                address=address,
                parent_id=parent_id,
                content=content,
                mentions=mentions,
                correlation_id=correlation_id,
                session=session,
            ),
            idempotency_key=idempotency_key,
            response_type=_POSTREPLYRESPONSE,
            expected={200, 201},
        )

    async def read_tree(
        self,
        *,
        address: str,
        cycle_id: str | None = None,
        limit: int = 1000,
        page_cursor: str | None = None,
        snapshot_cursor: str | None = None,
    ) -> TreeResponse:
        """Read one stable snapshot page of a cycle tree."""

        return await self._json_request(
            "POST",
            "/api/v1/tree",
            json=self._tree_body(
                address=address,
                cycle_id=cycle_id,
                limit=limit,
                page_cursor=page_cursor,
                snapshot_cursor=snapshot_cursor,
            ),
            response_type=_TREERESPONSE,
            expected={200},
        )

    async def read_message(
        self, *, address: str, message_id: str, cycle_id: str | None = None
    ) -> MessageReadResponse:
        """Read one message and its current direct-child count."""

        return await self._json_request(
            "POST",
            "/api/v1/tree/message",
            json=self._read_body(address=address, cycle_id=cycle_id, message_id=message_id),
            response_type=_MESSAGEREADRESPONSE,
            expected={200},
        )

    async def read_branch(
        self, *, address: str, leaf_id: str, cycle_id: str | None = None
    ) -> BranchResponse:
        """Read the ordered path from the root to one leaf."""

        return await self._json_request(
            "POST",
            "/api/v1/tree/branch",
            json=self._read_body(address=address, cycle_id=cycle_id, leaf_id=leaf_id),
            response_type=_BRANCHRESPONSE,
            expected={200},
        )

    async def read_replies(
        self,
        *,
        address: str,
        message_id: str,
        cycle_id: str | None = None,
        limit: int = 100,
        page_cursor: str | None = None,
        snapshot_cursor: str | None = None,
    ) -> RepliesResponse:
        """Read one stable page of direct replies to a message."""

        body = self._read_body(address=address, cycle_id=cycle_id, message_id=message_id)
        body["limit"] = limit
        if page_cursor:
            body["page_cursor"] = page_cursor
        if snapshot_cursor:
            body["snapshot_cursor"] = snapshot_cursor
        return await self._json_request(
            "POST",
            "/api/v1/tree/replies",
            json=body,
            response_type=_REPLIESRESPONSE,
            expected={200},
        )

    async def read_subtree(
        self,
        *,
        address: str,
        message_id: str,
        cycle_id: str | None = None,
        max_depth: int = 8,
        limit: int = 200,
        page_cursor: str | None = None,
        snapshot_cursor: str | None = None,
    ) -> SubtreeResponse:
        """Read one stable preorder page below a selected message."""

        body = self._tree_body(
            address=address,
            cycle_id=cycle_id,
            limit=limit,
            page_cursor=page_cursor,
            snapshot_cursor=snapshot_cursor,
        )
        body.update({"message_id": message_id, "max_depth": max_depth})
        return await self._json_request(
            "POST",
            "/api/v1/tree/subtree",
            json=body,
            response_type=_SUBTREERESPONSE,
            expected={200},
        )

    async def read_leaves(
        self,
        *,
        address: str,
        cycle_id: str | None = None,
        limit: int = 100,
        page_cursor: str | None = None,
        snapshot_cursor: str | None = None,
    ) -> LeavesResponse:
        """Read discussion fronts in one stable snapshot."""

        return await self._json_request(
            "POST",
            "/api/v1/tree/leaves",
            json=self._tree_body(
                address=address,
                cycle_id=cycle_id,
                limit=limit,
                page_cursor=page_cursor,
                snapshot_cursor=snapshot_cursor,
            ),
            response_type=_LEAVESRESPONSE,
            expected={200},
        )

    async def read_activity(
        self,
        *,
        address: str,
        after_cursor: str | None = None,
        wait_seconds: int = 0,
        limit: int = 100,
        event_types: list[str] | None = None,
    ) -> ActivityResponse:
        """Read or briefly wait for chronological tunnel activity."""

        return await self._json_request(
            "POST",
            "/api/v1/activity",
            json=self._activity_body(
                address=address,
                after_cursor=after_cursor,
                wait_seconds=wait_seconds,
                limit=limit,
                event_types=event_types,
            ),
            response_type=_ACTIVITYRESPONSE,
            expected={200},
        )

    async def commit_checkpoint(self, *, address: str, cursor: str) -> CheckpointResponse:
        """Save one processed activity position for this credential."""

        return await self._json_request(
            "POST",
            "/api/v1/activity/checkpoint",
            json={"address": address, "cursor": cursor},
            response_type=_CHECKPOINTRESPONSE,
            expected={200},
        )

    async def get_context(
        self,
        *,
        address: str,
        focus_message_id: str,
        cycle_id: str | None = None,
        after_cursor: str | None = None,
        snapshot_cursor: str | None = None,
        referenced_message_ids: list[str] | None = None,
        max_items: int = 200,
        max_bytes: int = 1_048_576,
    ) -> ContextResponse:
        """Build one deterministic bounded context packet."""

        return await self._json_request(
            "POST",
            "/api/v1/context",
            json=self._context_body(
                address=address,
                focus_message_id=focus_message_id,
                cycle_id=cycle_id,
                after_cursor=after_cursor,
                snapshot_cursor=snapshot_cursor,
                referenced_message_ids=referenced_message_ids,
                max_items=max_items,
                max_bytes=max_bytes,
            ),
            response_type=_CONTEXTRESPONSE,
            expected={200},
        )

    async def search_messages(
        self,
        *,
        address: str,
        query: str | None = None,
        cycle_id: str | None = None,
        sender_id: str | None = None,
        mentioned_agent_id: str | None = None,
        correlation_id: str | None = None,
        branch_root_id: str | None = None,
        sequence_after: int | None = None,
        sequence_before: int | None = None,
        page_cursor: str | None = None,
        limit: int = 50,
    ) -> SearchResponse:
        """Search one authorized cycle with bounded results."""

        return await self._json_request(
            "POST",
            "/api/v1/search",
            json=self._search_body(
                address=address,
                query=query,
                cycle_id=cycle_id,
                sender_id=sender_id,
                mentioned_agent_id=mentioned_agent_id,
                correlation_id=correlation_id,
                branch_root_id=branch_root_id,
                sequence_after=sequence_after,
                sequence_before=sequence_before,
                page_cursor=page_cursor,
                limit=limit,
            ),
            response_type=_SEARCHRESPONSE,
            expected={200},
        )

    async def list_participants(
        self,
        *,
        address: str,
        cycle_id: str | None = None,
        page_cursor: str | None = None,
        limit: int = 100,
    ) -> ParticipantsResponse:
        """List agents recorded in one cycle."""

        return await self._json_request(
            "POST",
            "/api/v1/participants",
            json=self._participants_body(
                address=address,
                cycle_id=cycle_id,
                page_cursor=page_cursor,
                limit=limit,
            ),
            response_type=_PARTICIPANTSRESPONSE,
            expected={200},
        )

    async def close_cycle(
        self, *, address: str, expected_cycle_id: str, idempotency_key: str
    ) -> None:
        """Close the active cycle without retiring its stable tunnel address."""

        await self._request(
            "POST",
            "/api/v1/cycles/close",
            json={"address": address, "expected_cycle_id": expected_cycle_id},
            idempotency_key=idempotency_key,
            expected={204},
        )

    async def get_tunnel_status(self, *, address: str) -> TunnelStatusResponse:
        """Read tunnel state without loading message content."""

        return await self._json_request(
            "POST",
            "/api/v1/tunnel/status",
            json={"address": address},
            response_type=_TUNNELSTATUSRESPONSE,
            expected={200},
        )

    async def list_cycles(
        self, *, address: str, page_cursor: str | None = None, limit: int = 50
    ) -> CycleListResponse:
        """List externally available cycles, newest first."""

        return await self._json_request(
            "POST",
            "/api/v1/cycles",
            json=self._cycle_list_body(address=address, page_cursor=page_cursor, limit=limit),
            response_type=_CYCLELISTRESPONSE,
            expected={200},
        )

    async def start_cycle(
        self,
        *,
        address: str,
        expected_address_generation: int,
        root_content: JsonObject,
        idempotency_key: str,
        cycle_label: str = "",
        expires_in_seconds: int | None = None,
        root_session: str | None = None,
    ) -> StartedCycleResponse:
        """Start a rooted cycle in a dormant tunnel."""

        return await self._json_request(
            "POST",
            "/api/v1/cycles/start",
            json=self._lifecycle_cycle_body(
                address=address,
                expected_address_generation=expected_address_generation,
                root_content=root_content,
                cycle_label=cycle_label,
                expires_in_seconds=expires_in_seconds,
                root_session=root_session,
            ),
            idempotency_key=idempotency_key,
            response_type=_STARTEDCYCLERESPONSE,
            expected={201},
        )

    async def rollover_cycle(
        self,
        *,
        address: str,
        expected_cycle_id: str,
        expected_address_generation: int,
        root_content: JsonObject,
        idempotency_key: str,
        cycle_label: str = "",
        expires_in_seconds: int | None = None,
        root_session: str | None = None,
    ) -> StartedCycleResponse:
        """Atomically replace the active cycle under the same address."""

        body = self._lifecycle_cycle_body(
            address=address,
            expected_address_generation=expected_address_generation,
            root_content=root_content,
            cycle_label=cycle_label,
            expires_in_seconds=expires_in_seconds,
            root_session=root_session,
        )
        body["expected_cycle_id"] = expected_cycle_id
        return await self._json_request(
            "POST",
            "/api/v1/cycles/rollover",
            json=body,
            idempotency_key=idempotency_key,
            response_type=_STARTEDCYCLERESPONSE,
            expected={201},
        )

    @staticmethod
    def _create_tunnel_body(
        *,
        label: str,
        root_content: JsonObject,
        cycle_label: str,
        expires_in_seconds: int | None,
        root_mentions: list[str] | None,
        root_correlation_id: str | None,
        root_session: str | None,
    ) -> JsonObject:
        root: JsonObject = {"content": root_content, "mentions": list(root_mentions or [])}
        if root_correlation_id is not None:
            root["correlation_id"] = root_correlation_id
        if root_session is not None:
            root["session"] = root_session
        cycle: JsonObject = {"label": cycle_label, "root": root}
        if expires_in_seconds is not None:
            cycle["expires_in_seconds"] = expires_in_seconds
        return {"label": label, "cycle": cycle}

    @staticmethod
    def _reply_body(
        *,
        address: str,
        parent_id: str,
        content: JsonObject,
        mentions: list[str] | None,
        correlation_id: str | None,
        session: str | None,
    ) -> JsonObject:
        body: JsonObject = {
            "address": address,
            "parent_id": parent_id,
            "content": content,
            "mentions": list(mentions or []),
        }
        if correlation_id is not None:
            body["correlation_id"] = correlation_id
        if session is not None:
            body["session"] = session
        return body

    @staticmethod
    def _tree_body(
        *,
        address: str,
        cycle_id: str | None,
        limit: int,
        page_cursor: str | None,
        snapshot_cursor: str | None,
    ) -> JsonObject:
        body: JsonObject = {"address": address, "limit": limit}
        if cycle_id is not None:
            body["cycle_id"] = cycle_id
        if page_cursor is not None:
            body["page_cursor"] = page_cursor
        if snapshot_cursor is not None:
            body["snapshot_cursor"] = snapshot_cursor
        return body

    @staticmethod
    def _activity_body(
        *,
        address: str,
        after_cursor: str | None,
        wait_seconds: int,
        limit: int,
        event_types: list[str] | None,
    ) -> JsonObject:
        body: JsonObject = {
            "address": address,
            "wait_seconds": wait_seconds,
            "limit": limit,
        }
        if after_cursor is not None:
            body["after_cursor"] = after_cursor
        if event_types is not None:
            body["event_types"] = [cast(JsonValue, value) for value in event_types]
        return body

    @staticmethod
    def _context_body(
        *,
        address: str,
        focus_message_id: str,
        cycle_id: str | None,
        after_cursor: str | None,
        snapshot_cursor: str | None,
        referenced_message_ids: list[str] | None,
        max_items: int,
        max_bytes: int,
    ) -> JsonObject:
        body: JsonObject = {
            "address": address,
            "focus_message_id": focus_message_id,
            "referenced_message_ids": [
                cast(JsonValue, value) for value in referenced_message_ids or []
            ],
            "max_items": max_items,
            "max_bytes": max_bytes,
        }
        if cycle_id is not None:
            body["cycle_id"] = cycle_id
        if after_cursor is not None:
            body["after_cursor"] = after_cursor
        if snapshot_cursor is not None:
            body["snapshot_cursor"] = snapshot_cursor
        return body

    @staticmethod
    def _search_body(
        *,
        address: str,
        query: str | None,
        cycle_id: str | None,
        sender_id: str | None,
        mentioned_agent_id: str | None,
        correlation_id: str | None,
        branch_root_id: str | None,
        sequence_after: int | None,
        sequence_before: int | None,
        page_cursor: str | None,
        limit: int,
    ) -> JsonObject:
        body: JsonObject = {"address": address, "limit": limit}
        optional: dict[str, str | int | None] = {
            "query": query,
            "cycle_id": cycle_id,
            "sender_id": sender_id,
            "mentioned_agent_id": mentioned_agent_id,
            "correlation_id": correlation_id,
            "branch_root_id": branch_root_id,
            "sequence_after": sequence_after,
            "sequence_before": sequence_before,
            "page_cursor": page_cursor,
        }
        for name, value in optional.items():
            if value is not None:
                body[name] = cast(JsonValue, value)
        return body

    @staticmethod
    def _participants_body(
        *,
        address: str,
        cycle_id: str | None,
        page_cursor: str | None,
        limit: int,
    ) -> JsonObject:
        body: JsonObject = {"address": address, "limit": limit}
        if cycle_id is not None:
            body["cycle_id"] = cycle_id
        if page_cursor is not None:
            body["page_cursor"] = page_cursor
        return body

    @staticmethod
    def _cycle_list_body(*, address: str, page_cursor: str | None, limit: int) -> JsonObject:
        body: JsonObject = {"address": address, "limit": limit}
        if page_cursor is not None:
            body["page_cursor"] = page_cursor
        return body

    @staticmethod
    def _lifecycle_cycle_body(
        *,
        address: str,
        expected_address_generation: int,
        root_content: JsonObject,
        cycle_label: str,
        expires_in_seconds: int | None,
        root_session: str | None,
    ) -> JsonObject:
        root: JsonObject = {"content": root_content, "mentions": []}
        if root_session is not None:
            root["session"] = root_session
        cycle: JsonObject = {
            "label": cycle_label,
            "root": root,
        }
        if expires_in_seconds is not None:
            cycle["expires_in_seconds"] = expires_in_seconds
        return {
            "address": address,
            "expected_address_generation": expected_address_generation,
            "cycle": cycle,
        }

    @staticmethod
    def _read_body(*, address: str, cycle_id: str | None, **identity: str) -> JsonObject:
        body: JsonObject = {"address": address, **identity}
        if cycle_id is not None:
            body["cycle_id"] = cycle_id
        return body

    async def _json_request[T](
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        idempotency_key: str | None = None,
        expected: set[int],
        response_type: TypeAdapter[T],
    ) -> T:
        response = await self._request(
            method,
            path,
            json=json,
            idempotency_key=idempotency_key,
            expected=expected,
        )
        try:
            return response_type.validate_python(self._as_object(response), strict=True)
        except ValidationError as error:
            raise StarTunnelError(
                status_code=response.status_code,
                code="invalid_response",
                message="The service returned an unexpected JSON response.",
                request_id=response.headers.get("X-Request-ID"),
            ) from error

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: JsonObject | None = None,
        idempotency_key: str | None = None,
        expected: set[int],
    ) -> httpx.Response:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                headers=headers,
            )
        except httpx.RequestError as error:
            raise StarTunnelError(
                status_code=0,
                code="connection_failed",
                message="The service could not be reached. Check the base URL and service health.",
                request_id=None,
            ) from error

        if response.status_code not in expected:
            raise self._error_from_response(response)
        return response

    @staticmethod
    def _as_object(response: httpx.Response) -> JsonObject:
        try:
            value: object = response.json()
        except ValueError as error:
            raise StarTunnelError(
                status_code=response.status_code,
                code="invalid_response",
                message="The service returned a response that was not JSON.",
                request_id=response.headers.get("X-Request-ID"),
            ) from error
        if not isinstance(value, dict):
            raise StarTunnelError(
                status_code=response.status_code,
                code="invalid_response",
                message="The service returned an unexpected JSON value.",
                request_id=response.headers.get("X-Request-ID"),
            )
        return json_object(value)

    @staticmethod
    def _error_from_response(response: httpx.Response) -> StarTunnelError:
        request_id = response.headers.get("X-Request-ID")
        code = "request_failed"
        message = "The request failed."
        try:
            body: JsonValue = json_value(response.json())
        except ValueError:
            body = None
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            error = body["error"]
            assert isinstance(error, dict)
            code_value = error.get("code")
            if isinstance(code_value, str):
                code = code_value
            message_value = error.get("message")
            if isinstance(message_value, str):
                message = message_value
            request_id_value = error.get("request_id")
            if isinstance(request_id_value, str):
                request_id = request_id_value
        return StarTunnelError(
            status_code=response.status_code,
            code=code,
            message=message,
            request_id=request_id,
        )
