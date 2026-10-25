"""Typed wire records used by API test helpers."""

from typing import Literal, TypedDict

from pydantic import TypeAdapter


class CreatedTunnelData(TypedDict):
    id: str
    label: str
    address: str
    display_address: str
    address_transcription: str
    created_at: str
    state: Literal["active", "dormant", "retired"]


class RootSummaryData(TypedDict):
    id: str
    parent_id: None
    sequence: int
    depth: int


class CycleSummaryData(TypedDict):
    id: str
    number: int
    label: str
    state: str
    created_at: str
    expires_at: str
    root: RootSummaryData
    message_count: int
    content_bytes: int


class CreateResponseData(TypedDict):
    tunnel: CreatedTunnelData
    cycle: CycleSummaryData
    activity_cursor: str
    trust_notice: str
    idempotent_replay: bool


class PostedMessageData(TypedDict):
    id: str
    cycle_id: str
    cycle_number: int
    parent_id: str
    sequence: int
    depth: int
    created_at: str


class ReplyResponseData(TypedDict):
    message: PostedMessageData
    activity_cursor: str
    idempotent_replay: bool


class FieldErrorData(TypedDict):
    field: str
    message: str


class ErrorBodyData(TypedDict):
    code: str
    message: str
    request_id: str
    field_errors: list[FieldErrorData]


class ErrorResponseData(TypedDict):
    error: ErrorBodyData


_CREATE_RESPONSE = TypeAdapter(CreateResponseData)
_REPLY_RESPONSE = TypeAdapter(ReplyResponseData)
_ERROR_RESPONSE = TypeAdapter(ErrorResponseData)


def create_response(value: object) -> CreateResponseData:
    return _CREATE_RESPONSE.validate_python(value, strict=True)


def reply_response(value: object) -> ReplyResponseData:
    return _REPLY_RESPONSE.validate_python(value, strict=True)


def error_response(value: object) -> ErrorResponseData:
    return _ERROR_RESPONSE.validate_python(value, strict=True)
