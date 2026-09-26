"""Request metadata and browser security headers."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import Token
from ipaddress import ip_address, ip_network
from typing import Any, cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.conf import settings
from django.db import connection
from django.http import HttpRequest, HttpResponse

from .logging import request_id_var
from .metrics import API_REQUEST_SECONDS, API_REQUESTS, DB_OPERATION_SECONDS
from .security import CONTENT_SECURITY_POLICY, PERMISSIONS_POLICY

DATABASE_OPERATIONS = {"delete", "insert", "select", "update"}
SAFE_HTTP_METHODS = {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
logger = logging.getLogger("startunnel.request")


class _DualModeMiddleware:
    """Keep one middleware implementation usable in WSGI and ASGI."""

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponse | Awaitable[HttpResponse]],
    ) -> None:
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(cast(Any, self))

    def _sync_response(self, request: HttpRequest) -> HttpResponse:
        return cast(HttpResponse, self.get_response(request))

    async def _async_response(self, request: HttpRequest) -> HttpResponse:
        return await cast(Awaitable[HttpResponse], self.get_response(request))


class TrustedProxyMiddleware(_DualModeMiddleware):
    """Discard forwarding claims unless the direct peer is an approved proxy."""

    forwarded_fields = (
        "HTTP_FORWARDED",
        "HTTP_X_FORWARDED_FOR",
        "HTTP_X_FORWARDED_HOST",
        "HTTP_X_FORWARDED_PORT",
        "HTTP_X_FORWARDED_PROTO",
    )

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponse | Awaitable[HttpResponse]],
    ) -> None:
        super().__init__(get_response)
        try:
            self.networks = tuple(
                ip_network(value, strict=False) for value in settings.STARTUNNEL_TRUSTED_PROXY_CIDRS
            )
        except ValueError as error:
            raise RuntimeError(
                "STARTUNNEL_TRUSTED_PROXY_CIDRS contains an invalid network."
            ) from error

    def _prepare_request(self, request: HttpRequest) -> None:
        remote_address = request.META.get("REMOTE_ADDR", "")
        try:
            remote = ip_address(remote_address)
        except ValueError:
            trusted = False
        else:
            trusted = any(remote in network for network in self.networks)
        request.startunnel_trusted_proxy = trusted  # type: ignore[attr-defined]
        if not trusted:
            for field in self.forwarded_fields:
                request.META.pop(field, None)

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        self._prepare_request(request)
        return self._sync_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        self._prepare_request(request)
        return await self._async_response(request)


def _measure_database(
    execute: Callable[..., Any],
    sql: str,
    params: object,
    many: bool,
    context: object,
) -> Any:
    first_word = sql.lstrip().split(None, 1)[0].lower() if sql.strip() else "other"
    operation = first_word if first_word in DATABASE_OPERATIONS else "other"
    with DB_OPERATION_SECONDS.labels(operation=operation).time():
        return execute(sql, params, many, context)


class RequestIdMiddleware(_DualModeMiddleware):
    @staticmethod
    def _begin(request: HttpRequest) -> tuple[Token[str], float]:
        request_id = str(uuid.uuid4())
        request.request_id = request_id  # type: ignore[attr-defined]
        return request_id_var.set(request_id), time.monotonic()

    @staticmethod
    def _complete(request: HttpRequest, response: HttpResponse, started_at: float) -> HttpResponse:
        resolver_match = getattr(request, "resolver_match", None)
        operation = str(getattr(resolver_match, "view_name", "unmatched") or "unmatched")
        status_class = f"{response.status_code // 100}xx"
        duration_seconds = time.monotonic() - started_at
        response["X-Request-ID"] = str(request.request_id)  # type: ignore[attr-defined]
        if request.path.startswith("/api/"):
            response["Cache-Control"] = "no-store"
            API_REQUESTS.labels(operation=operation, status_class=status_class).inc()
            API_REQUEST_SECONDS.labels(
                operation=operation,
                status_class=status_class,
            ).observe(duration_seconds)
        logger.info(
            "Request completed.",
            extra={
                "route_name": operation,
                "method": request.method if request.method in SAFE_HTTP_METHODS else "OTHER",
                "status": response.status_code,
                "duration_ms": round(duration_seconds * 1_000, 3),
            },
        )
        return response

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        token, started_at = self._begin(request)
        try:
            return self._complete(request, self._sync_response(request), started_at)
        finally:
            request_id_var.reset(token)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        token, started_at = self._begin(request)
        try:
            return self._complete(request, await self._async_response(request), started_at)
        finally:
            request_id_var.reset(token)


class DatabaseMetricsMiddleware(_DualModeMiddleware):
    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        with connection.execute_wrapper(_measure_database):
            return self._sync_response(request)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        with connection.execute_wrapper(_measure_database):
            return await self._async_response(request)


class SecurityHeadersMiddleware(_DualModeMiddleware):
    @staticmethod
    def _add_headers(response: HttpResponse) -> HttpResponse:
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response["Permissions-Policy"] = PERMISSIONS_POLICY
        response["X-Frame-Options"] = "DENY"
        return response

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        return self._add_headers(self._sync_response(request))

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        return self._add_headers(await self._async_response(request))
