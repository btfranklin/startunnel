"""Keep every versioned API failure inside the public JSON error contract."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.http import HttpRequest, HttpResponse, JsonResponse

ERRORS: dict[int, tuple[str, str]] = {
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
        payload: Any = json.loads(response.content)
    except TypeError, ValueError:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("error"), dict)


class ApiErrorEnvelopeMiddleware:
    """Replace framework HTML or detail errors with the stable API envelope."""

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

    def __call__(self, request: HttpRequest) -> Any:
        if self.async_mode:
            return self.__acall__(request)
        response = cast(HttpResponse, self.get_response(request))
        return self._replace(request, response)

    async def __acall__(self, request: HttpRequest) -> HttpResponse:
        response = await cast(Awaitable[HttpResponse], self.get_response(request))
        return self._replace(request, response)
