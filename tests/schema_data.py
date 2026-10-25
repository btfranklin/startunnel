"""Validate the OpenAPI and CLI schema fields used by contract tests."""

from __future__ import annotations

from typing import NotRequired, TypedDict, cast

from pydantic import TypeAdapter
from scripts.json_data import JsonValue

SchemaReference = TypedDict("SchemaReference", {"$ref": str}, total=False)


class Schema(SchemaReference, total=False):
    type: str | list[str]
    const: JsonValue
    enum: list[JsonValue]
    anyOf: list[Schema]
    oneOf: list[Schema]
    allOf: list[Schema]
    properties: dict[str, Schema]
    required: list[str]
    items: Schema
    format: str
    default: JsonValue
    additionalProperties: bool | Schema
    description: str
    schema: Schema
    headers: dict[str, Schema]
    content: dict[str, MediaType]


class MediaType(TypedDict):
    schema: Schema


Parameter = TypedDict(
    "Parameter", {"in": str, "name": str, "required": bool, "schema": Schema}, total=False
)


class Operation(TypedDict):
    responses: dict[str, Schema]
    requestBody: NotRequired[RequestBody]
    parameters: NotRequired[list[Parameter]]


class RequestBody(TypedDict):
    content: dict[str, MediaType]
    required: NotRequired[bool]


class OpenApiDocument(TypedDict):
    paths: dict[str, dict[str, Operation]]
    components: dict[str, dict[str, Schema]]


class CliSchemaCommand(TypedDict):
    command: str
    method: str
    path: str
    stdin_json: bool
    idempotency_key_required: bool
    required_fields: list[str]
    optional_fields: list[str]
    example: NotRequired[JsonValue]
    success_statuses: NotRequired[list[int]]
    responses: dict[str, Schema]
    stdout_schemas: dict[str, Schema]
    secret_output_required: NotRequired[bool]


class CliSchemaOutput(TypedDict):
    commands: list[CliSchemaCommand]
    components: dict[str, dict[str, Schema]]
    error_output: ErrorOutput


class ErrorOutput(TypedDict):
    stream: str
    schema: Schema


_OPENAPI = TypeAdapter(OpenApiDocument)
_CLI_SCHEMA = TypeAdapter(CliSchemaOutput)


def openapi_document(value: object) -> OpenApiDocument:
    _OPENAPI.validate_python(value, strict=True)
    # Keep all published fields so comparisons also detect fields outside this test interface.
    return cast(OpenApiDocument, value)


def schema_output(value: object) -> CliSchemaOutput:
    _CLI_SCHEMA.validate_python(value, strict=True)
    return cast(CliSchemaOutput, value)
