"""Typed ASGI scope and response helpers for middleware tests."""

from asgiref.typing import (
    ASGISendEvent,
    HTTPResponseBodyEvent,
    HTTPResponseStartEvent,
    HTTPScope,
)


def http_scope(*, path: str, method: str, headers: list[tuple[bytes, bytes]]) -> HTTPScope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.5"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "extensions": {},
    }


def response_start(event: ASGISendEvent) -> HTTPResponseStartEvent:
    assert event["type"] == "http.response.start"
    return event


def response_body(event: ASGISendEvent) -> HTTPResponseBodyEvent:
    assert event["type"] == "http.response.body"
    return event
