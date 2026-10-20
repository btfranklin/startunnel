"""Keep shared CLI transport headers in the public API definition."""

from __future__ import annotations

from typing import Any

from ninja import NinjaAPI
from ninja.openapi.schema import OpenAPISchema

from .client_compatibility import CLIENT_VERSION_HEADER, response_headers


class ClientAwareAPI(NinjaAPI):
    def get_openapi_schema(
        self,
        *,
        path_prefix: str | None = None,
        path_params: dict[str, Any] | None = None,
    ) -> OpenAPISchema:
        schema = super().get_openapi_schema(path_prefix=path_prefix, path_params=path_params)
        headers = response_headers()
        schema["components"]["headers"] = headers
        for path in schema["paths"].values():
            for method, operation in path.items():
                if method not in {"get", "post"}:
                    continue
                operation.setdefault("parameters", []).append(
                    {
                        "in": "header",
                        "name": CLIENT_VERSION_HEADER,
                        "required": False,
                        "description": (
                            "Required for the official CLI. Use its release label. "
                            "Source builds send unreleased and remain unverified. "
                            "Other API clients can omit this header."
                        ),
                        "schema": {"type": "string", "maxLength": 64},
                    }
                )
                for response in operation["responses"].values():
                    response.setdefault("headers", {}).update(
                        {name: {"$ref": "#/components/headers/" + name} for name in headers}
                    )
        return schema
