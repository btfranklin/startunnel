"""Keep every versioned API failure inside the public JSON error contract."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse, JsonResponse

from core.errors import PublicErrorCode

from .client_compatibility import (
    add_compatibility_headers,
    compatibility,
    is_api_path,
    update_error,
)

ERRORS: dict[int, tuple[PublicErrorCode, str]] = {
    400: ("invalid_request", "The request is not valid. Correct it and try again."),
    401: ("invalid_credential", "The agent credential is not valid."),
    403: ("invalid_credential", "The agent credential is not valid."),
    404: ("invalid_request", "This API route is not available."),
    405: ("invalid_request", "This HTTP method is not available for the API route."),
    413: ("payload_too_large", "The message is larger than the allowed size."),
    429: ("rate_limited", "Too many requests were sent. Wait and try again."),
    500: ("internal_error", "An unexpected server error occurred."),
    503: ("dependency_unavailable", "A required service is unavailable. Try again later."),
}


def _has_error_envelope(response: HttpResponse) -> bool:
    if not response.get("Content-Type", "").startswith("application/json"):
        return False
    try:
        payload: object = json.loads(response.content)
    except TypeError, ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("error"), dict)


class ApiErrorEnvelopeMiddleware:
    """Check CLI compatibility and keep errors inside the public API envelope."""

    sync_capable = True
    async_capable = True

    def __init__(
        self,
        get_response: Callable[[HttpRequest], HttpResponse | Awaitable[HttpResponse]],
    ) -> None:
        self.get_response = get_response
        self.async_mode = iscoroutinefunction(get_response)
        if self.async_mode:
            markcoroutinefunction(
                cast(Callable[[HttpRequest], HttpResponse | Awaitable[HttpResponse]], self)
            )

    @staticmethod
    def _replace(request: HttpRequest, response: HttpResponse) -> HttpResponse:
        if (
            not request.path.startswith("/api/v1")
            or response.status_code < 400
            or _has_error_envelope(response)
        ):
            return response

        status = (
            500
            if response.status_code >= 500 and response.status_code != 503
            else response.status_code
        )
        code, message = ERRORS.get(status, ERRORS[400])
        replacement = JsonResponse(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": str(getattr(request, "request_id", "unknown")),
                    "field_errors": [],
                }
            },
            status=status,
        )
        for header in ("Allow", "Retry-After"):
            if header in response:
                replacement[header] = response[header]
        return replacement

    def __call__(self, request: HttpRequest) -> HttpResponse | Awaitable[HttpResponse]:
        if self.async_mode:
            return self.__acall__(request)
        response = (
            update_error(request)
            if is_api_path(request.path) and compatibility(request) == "update_required"
            else cast(HttpResponse, self.get_response(request))
        )
        return self._complete(request, response)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        response = (
            update_error(request)
            if is_api_path(request.path) and compatibility(request) == "update_required"
            else await cast(Awaitable[HttpResponse], self.get_response(request))
        )
        return self._complete(request, response)

    @classmethod
    def _complete(cls, request: HttpRequest, response: HttpResponse) -> HttpResponse:
        response = cls._replace(request, response)
        if is_api_path(request.path):
            add_compatibility_headers(request, response)
        return response
