"""Report CLI compatibility before an API operation can run."""

from __future__ import annotations

import re
from typing import Literal

from django.http import HttpRequest, HttpResponse, JsonResponse

from core.build import BUILD

MINIMUM_CLIENT_VERSION = "v0.3.0"
CLIENT_VERSION_HEADER = "X-StarTunnel-Client-Version"
SERVER_VERSION_HEADER = "X-StarTunnel-Server-Version"
MINIMUM_VERSION_HEADER = "X-StarTunnel-Minimum-Client-Version"
COMPATIBILITY_HEADER = "X-StarTunnel-Client-Compatibility"
RELEASE_VERSION = re.compile(r"v(0|[1-9][0-9]{0,17})\.(0|[1-9][0-9]{0,17})\.(0|[1-9][0-9]{0,17})")
Compatibility = Literal["supported", "update_required", "unverified", "not_checked"]


def is_api_path(path: str) -> bool:
    """Limit compatibility checks to the public API namespace."""

    return path == "/api/v1" or path.startswith("/api/v1/")


def release_version(value: str) -> tuple[int, int, int] | None:
    """Parse a bounded stable release label without reflecting its value."""

    match = RELEASE_VERSION.fullmatch(value) if len(value) <= 64 else None
    return (int(match[1]), int(match[2]), int(match[3])) if match is not None else None


def compatibility(request: HttpRequest) -> Compatibility:
    """Compare self-reported versions without treating them as authentication."""

    supplied = request.headers.get(CLIENT_VERSION_HEADER)
    if supplied is None:
        official_cli = request.headers.get("User-Agent", "").split("/", 1)[0]
        return "update_required" if official_cli == "startunnel-agent-cli" else "not_checked"
    if supplied == "unreleased":
        return "unverified"
    version = release_version(supplied)
    minimum = release_version(MINIMUM_CLIENT_VERSION)
    assert minimum is not None
    return "supported" if version is not None and version >= minimum else "update_required"


def update_error(request: HttpRequest) -> JsonResponse:
    """Use a code that released clients already display with its safe message."""

    return JsonResponse(
        {
            "error": {
                "code": "invalid_request",
                "message": (
                    f"This server requires StarTunnel CLI {MINIMUM_CLIENT_VERSION} or later. "
                    "Run startunnel update, then startunnel me or startunnel admin me. "
                    "If update is unavailable, download /downloads/star_tunnel.py "
                    "from this instance."
                ),
                "request_id": str(getattr(request, "request_id", "unknown")),
                "field_errors": [
                    {
                        "field": CLIENT_VERSION_HEADER,
                        "message": "Update the CLI before retrying this request.",
                    }
                ],
            }
        },
        status=400,
    )


def add_compatibility_headers(request: HttpRequest, response: HttpResponse) -> None:
    """Keep version information separate from successful JSON response bodies."""

    version = BUILD["version"]
    response[SERVER_VERSION_HEADER] = (
        version
        if isinstance(version, str) and release_version(version) is not None
        else "unreleased"
    )
    response[MINIMUM_VERSION_HEADER] = MINIMUM_CLIENT_VERSION
    response[COMPATIBILITY_HEADER] = compatibility(request)


def response_headers() -> dict[str, object]:
    """Describe headers added by the API transport in the authoritative schema."""

    return {
        SERVER_VERSION_HEADER: {
            "description": "Server release label, or unreleased for a source or custom build.",
            "schema": {"type": "string"},
        },
        MINIMUM_VERSION_HEADER: {
            "description": "Minimum supported official CLI release.",
            "schema": {"type": "string", "enum": [MINIMUM_CLIENT_VERSION]},
        },
        COMPATIBILITY_HEADER: {
            "description": (
                "Self-reported CLI compatibility. Unreleased builds are unverified. "
                "Other API clients without a version are not_checked. This is not authentication."
            ),
            "schema": {
                "type": "string",
                "enum": ["supported", "update_required", "unverified", "not_checked"],
            },
        },
    }
