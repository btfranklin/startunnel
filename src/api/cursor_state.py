"""Named results from API cursor validation."""

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True, kw_only=True)
class PageCursor:
    cycle_id: UUID | None = None
    after_sequence: int = 0
    snapshot_sequence: int | None = None
    expected_tunnel_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextCursor:
    cycle_id: UUID | None = None
    snapshot_sequence: int | None = None
    activity_position: int | None = None
    expected_tunnel_id: UUID | None = None
    after_position: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SearchCursor:
    cycle_id: UUID | None = None
    snapshot_sequence: int | None = None
    after_rank: int | None = None
    after_sequence: int | None = None
    after_message_id: UUID | None = None
    expected_tunnel_id: UUID | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ParticipantCursor:
    cycle_id: UUID | None = None
    snapshot_sequence: int | None = None
    after_first_sequence: int | None = None
    after_credential_id: UUID | None = None
    expected_tunnel_id: UUID | None = None
