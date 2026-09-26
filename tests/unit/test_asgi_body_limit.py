"""The ASGI boundary rejects a large body before authentication or Django."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from core.asgi import ASGIReceive, ASGISend, RequestBodyLimitMiddleware


def _scope(*, content_length: int | None = None) -> dict[str, Any]:
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/messages",
        "raw_path": b"/api/v1/messages",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }


async def _run(
    *,
    content_length: int | None,
    messages: list[dict[str, Any]],
    maximum: int = 16,
) -> tuple[bool, list[dict[str, Any]]]:
    called = False
    sent: list[dict[str, Any]] = []
    iterator: AsyncIterator[dict[str, Any]]

    async def events() -> AsyncIterator[dict[str, Any]]:
        for message in messages:
            yield message

    iterator = events()

    async def receive() -> dict[str, Any]:
        return await anext(iterator)

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(dict(message))

    async def downstream(scope: dict[str, Any], receive: ASGIReceive, send: ASGISend) -> None:
        nonlocal called
        del scope, receive, send
        called = True

    middleware = RequestBodyLimitMiddleware(downstream, maximum_bytes=maximum)
    await middleware(_scope(content_length=content_length), receive, send)
    return called, sent


@pytest.mark.asyncio
async def test_declared_large_body_is_rejected_before_downstream() -> None:
    called, sent = await _run(
        content_length=17,
        messages=[{"type": "http.request", "body": b"", "more_body": False}],
    )

    assert not called
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["error"]["code"] == "payload_too_large"
    headers = dict(sent[0]["headers"])
    assert body["error"]["request_id"] == headers[b"x-request-id"].decode()
    assert headers[b"cache-control"] == b"no-store"


@pytest.mark.asyncio
async def test_chunked_large_body_is_rejected_before_downstream() -> None:
    called, sent = await _run(
        content_length=None,
        messages=[
            {"type": "http.request", "body": b"1234567890", "more_body": True},
            {"type": "http.request", "body": b"1234567", "more_body": False},
        ],
    )

    assert not called
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_bounded_body_replays_to_downstream() -> None:
    called, sent = await _run(
        content_length=4,
        messages=[{"type": "http.request", "body": b"safe", "more_body": False}],
    )

    assert called
    assert sent == []
