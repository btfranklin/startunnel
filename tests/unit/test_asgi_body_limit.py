"""The ASGI boundary rejects a large body before authentication or Django."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
from asgiref.typing import ASGIReceiveEvent, ASGISendEvent, HTTPRequestEvent, HTTPScope, Scope
from tests.asgi import http_scope, response_body, response_start

from core.asgi import ASGIReceive, ASGISend, RequestBodyLimitMiddleware


def _scope(*, content_length: int | None = None) -> HTTPScope:
    headers = [] if content_length is None else [(b"content-length", str(content_length).encode())]
    return http_scope(path="/api/v1/messages", method="POST", headers=headers)


async def _run(
    *,
    content_length: int | None,
    messages: list[HTTPRequestEvent],
    maximum: int = 16,
) -> tuple[bool, list[ASGISendEvent]]:
    called = False
    sent: list[ASGISendEvent] = []
    iterator: AsyncIterator[ASGIReceiveEvent]

    async def events() -> AsyncIterator[ASGIReceiveEvent]:
        for message in messages:
            yield message

    iterator = events()

    async def receive() -> ASGIReceiveEvent:
        return await anext(iterator)

    async def send(message: ASGISendEvent) -> None:
        sent.append(message)

    async def downstream(scope: Scope, receive: ASGIReceive, send: ASGISend) -> None:
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
    assert response_start(sent[0])["status"] == 413
    body = json.loads(response_body(sent[1])["body"])
    assert body["error"]["code"] == "payload_too_large"
    headers = dict(response_start(sent[0])["headers"])
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
    assert response_start(sent[0])["status"] == 413


@pytest.mark.asyncio
async def test_bounded_body_replays_to_downstream() -> None:
    called, sent = await _run(
        content_length=4,
        messages=[{"type": "http.request", "body": b"safe", "more_body": False}],
    )

    assert called
    assert sent == []
