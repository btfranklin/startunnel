"""The ASGI static boundary serves assets without a synchronous middleware hop."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from core.asgi import ASGIReceive, ASGISend, StaticFilesMiddleware


async def _request(
    root: Path,
    *,
    path: str,
    method: str = "GET",
    headers: list[tuple[bytes, bytes]] | None = None,
    find_file: Any = None,
) -> tuple[bool, list[dict[str, Any]]]:
    downstream_called = False
    sent: list[dict[str, Any]] = []
    events: AsyncIterator[dict[str, Any]]

    async def request_events() -> AsyncIterator[dict[str, Any]]:
        yield {"type": "http.request", "body": b"", "more_body": False}

    events = request_events()

    async def receive() -> dict[str, Any]:
        return await anext(events)

    async def send(message: Mapping[str, Any]) -> None:
        sent.append(dict(message))

    async def downstream(scope: dict[str, Any], receive: ASGIReceive, send: ASGISend) -> None:
        nonlocal downstream_called
        del scope, receive, send
        downstream_called = True

    middleware = StaticFilesMiddleware(
        downstream,
        root=root,
        url_prefix="/static/",
        find_file=find_file,
    )
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
    }
    await middleware(scope, receive, send)
    return downstream_called, sent


@pytest.mark.asyncio
async def test_static_file_has_type_length_cache_and_security_headers(tmp_path: Path) -> None:
    asset = tmp_path / "css" / "site.css"
    asset.parent.mkdir()
    asset.write_bytes(b"body { color: navy; }")

    downstream_called, sent = await _request(tmp_path, path="/static/css/site.css")

    assert downstream_called is False
    assert sent[0]["status"] == 200
    headers = dict(sent[0]["headers"])
    assert headers[b"content-type"] == b"text/css"
    assert headers[b"content-length"] == str(asset.stat().st_size).encode()
    assert headers[b"cache-control"] == b"no-cache"
    assert headers[b"content-security-policy"]
    assert headers[b"permissions-policy"]
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert sent[1]["body"] == asset.read_bytes()


@pytest.mark.asyncio
async def test_hashed_static_file_is_immutable_and_supports_head_and_etag(tmp_path: Path) -> None:
    asset = tmp_path / "app.0123456789ab.js"
    asset.write_bytes(b"export const ready = true;")

    _, first = await _request(tmp_path, path="/static/app.0123456789ab.js", method="HEAD")
    first_headers = dict(first[0]["headers"])
    assert first_headers[b"cache-control"] == b"public, max-age=31536000, immutable"
    assert first[1]["body"] == b""

    _, cached = await _request(
        tmp_path,
        path="/static/app.0123456789ab.js",
        headers=[(b"if-none-match", first_headers[b"etag"])],
    )
    assert cached[0]["status"] == 304
    assert cached[1]["body"] == b""


@pytest.mark.asyncio
async def test_static_source_finder_is_a_development_fallback(tmp_path: Path) -> None:
    collected = tmp_path / "collected"
    collected.mkdir()
    source = tmp_path / "source.css"
    source.write_bytes(b"body { margin: 0; }")

    downstream_called, sent = await _request(
        collected,
        path="/static/source.css",
        find_file=lambda relative: str(source) if relative == "source.css" else None,
    )

    assert downstream_called is False
    assert sent[0]["status"] == 200
    assert sent[1]["body"] == source.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/static/missing.css", "/static/../private.txt", "/app/"])
async def test_missing_unsafe_and_non_static_paths_reach_django(
    tmp_path: Path,
    path: str,
) -> None:
    downstream_called, sent = await _request(tmp_path, path=path)

    assert downstream_called is True
    assert sent == []
