"""Small ASGI guards that run before Django allocates a request body."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from email.utils import formatdate
from pathlib import Path
from typing import Any

from .security import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY

ASGIMessage = Mapping[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApplication = Callable[[dict[str, Any], ASGIReceive, ASGISend], Awaitable[None]]
StaticFinder = Callable[[str], str | list[str] | None]
_HASHED_ASSET = re.compile(r"\.[0-9a-f]{8,64}\.", flags=re.IGNORECASE)


class StaticFilesMiddleware:
    """Serve collected or source static files without adapting ASGI to WSGI."""

    def __init__(
        self,
        application: ASGIApplication,
        *,
        root: Path,
        url_prefix: str,
        find_file: StaticFinder | None = None,
    ) -> None:
        self.application = application
        self.root = root.resolve()
        self.url_prefix = "/" + url_prefix.strip("/") + "/"
        self.find_file = find_file

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        path = str(scope.get("path", ""))
        if (
            scope.get("type") != "http"
            or scope.get("method") not in {"GET", "HEAD"}
            or not path.startswith(self.url_prefix)
        ):
            await self.application(scope, receive, send)
            return
        relative = path.removeprefix(self.url_prefix)
        file_path = await asyncio.to_thread(self._find, relative)
        if file_path is None:
            await self.application(scope, receive, send)
            return
        await self._send_file(scope, send, file_path)

    def _find(self, relative: str) -> Path | None:
        if not relative or "\\" in relative:
            return None
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        if candidate.is_file():
            return candidate
        if self.find_file is None:
            return None
        found = self.find_file(relative)
        if not isinstance(found, str):
            return None
        source = Path(found).resolve()
        return source if source.is_file() else None

    async def _send_file(
        self,
        scope: dict[str, Any],
        send: ASGISend,
        file_path: Path,
    ) -> None:
        status = await asyncio.to_thread(file_path.stat)
        etag = f'"{status.st_mtime_ns:x}-{status.st_size:x}"'
        request_headers = {name.lower(): value for name, value in scope.get("headers", [])}
        cache_control = (
            b"public, max-age=31536000, immutable"
            if _HASHED_ASSET.search(file_path.name)
            else b"no-cache"
        )
        common_headers = [
            (b"cache-control", cache_control),
            (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
            (b"etag", etag.encode()),
            (
                b"last-modified",
                formatdate(status.st_mtime, usegmt=True).encode(),
            ),
            (b"permissions-policy", PERMISSIONS_POLICY.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
        ]
        if request_headers.get(b"if-none-match") == etag.encode():
            await send({"type": "http.response.start", "status": 304, "headers": common_headers})
            await send({"type": "http.response.body", "body": b""})
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        headers = [
            *common_headers,
            (b"content-length", str(status.st_size).encode()),
            (b"content-type", content_type.encode()),
        ]
        body = (
            b"" if scope.get("method") == "HEAD" else await asyncio.to_thread(file_path.read_bytes)
        )
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class RequestBodyLimitMiddleware:
    """Buffer at most one small request body before Django or authentication."""

    def __init__(self, application: ASGIApplication, *, maximum_bytes: int) -> None:
        self.application = application
        self.maximum_bytes = maximum_bytes

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return
        if self._declared_size(scope) > self.maximum_bytes:
            await self._send_too_large(scope, send)
            return

        messages: list[ASGIMessage] = []
        size = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.maximum_bytes:
                    await self._send_too_large(scope, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        next_message = 0

        async def replay_receive() -> ASGIMessage:
            nonlocal next_message
            if next_message < len(messages):
                message = messages[next_message]
                next_message += 1
                return message
            return await receive()

        await self.application(scope, replay_receive, send)

    @staticmethod
    def _declared_size(scope: dict[str, Any]) -> int:
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    return max(int(value), 0)
                except ValueError:
                    return 0
        return 0

    @staticmethod
    async def _send_too_large(scope: dict[str, Any], send: ASGISend) -> None:
        request_id = str(uuid.uuid4())
        api_request = str(scope.get("path", "")).startswith("/api/v1/")
        if api_request:
            body = json.dumps(
                {
                    "error": {
                        "code": "payload_too_large",
                        "message": "The request body is too large.",
                        "request_id": request_id,
                        "field_errors": [],
                    }
                },
                separators=(",", ":"),
            ).encode()
            content_type = b"application/json"
        else:
            body = b"The request body is too large."
            content_type = b"text/plain; charset=utf-8"
        headers = [
            (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode()),
            (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
            (b"content-type", content_type),
            (b"permissions-policy", PERMISSIONS_POLICY.encode()),
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"x-request-id", request_id.encode()),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})


class LifespanMiddleware:
    """Own process-scoped listeners and complete ASGI lifespan requests."""

    def __init__(self, application: ASGIApplication) -> None:
        self.application = application

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "lifespan":
            await self.application(scope, receive, send)
            return
        from .activity_listener import activity_listener

        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                activity_listener.start()
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await activity_listener.stop()
                await send({"type": "lifespan.shutdown.complete"})
                return
