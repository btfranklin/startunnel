"""Generate the standalone CLI's API response contract from OpenAPI."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "generated" / "openapi.json"
CLIENT = ROOT / "cli" / "star_tunnel.py"

BEGIN_MARKER = "# BEGIN GENERATED RESPONSE CONTRACT"
END_MARKER = "# END GENERATED RESPONSE CONTRACT"
COMPONENT_REF_PREFIX = "#/components/"
SUPPORTED_COMPONENT_GROUPS = {"schemas", "headers"}
API_PATH_PREFIX = "/api/"
HTTP_METHODS = {"get", "post"}


def _component_reference(reference: str) -> tuple[str, str]:
    if not reference.startswith(COMPONENT_REF_PREFIX):
        raise ValueError(
            f"response contract reference {reference!r} is outside supported component groups"
        )
    group, separator, escaped_name = reference.removeprefix(COMPONENT_REF_PREFIX).partition("/")
    if not separator or group not in SUPPORTED_COMPONENT_GROUPS:
        raise ValueError(
            f"response contract reference {reference!r} is outside supported component groups"
        )
    name = escaped_name.replace("~1", "/").replace("~0", "~")
    if not name:
        raise ValueError(f"response contract reference {reference!r} has no component name")
    return group, name


def _references(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str):
                raise ValueError(f"response contract reference must be a string: {reference!r}")
            found.append(reference)
        for child in value.values():
            found.extend(_references(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_references(child))
    return found


def _factor_responses(operations: dict[str, Any]) -> dict[str, Any]:
    """Share identical response headers and error definitions."""

    responses = [response for operation in operations.values() for response in operation.values()]
    common_headers = dict(responses[0].get("headers", {})) if responses else {}
    for response in responses[1:]:
        headers = response.get("headers", {})
        common_headers = {
            name: value
            for name, value in common_headers.items()
            if name in headers and headers[name] == value
        }

    errors: dict[str, Any] = {}
    compact: dict[str, Any] = {}
    for operation, definitions in sorted(operations.items()):
        compact[operation] = {}
        for status, response in sorted(definitions.items()):
            reduced = dict(response)
            if common_headers:
                headers = {
                    name: value
                    for name, value in response["headers"].items()
                    if name not in common_headers
                }
                if headers:
                    reduced["headers"] = headers
                else:
                    reduced.pop("headers")
            if status.startswith(("4", "5")):
                shared = errors.setdefault(status, reduced)
                compact[operation][status] = status if reduced == shared else reduced
            else:
                compact[operation][status] = reduced
    return {"common_headers": common_headers, "error_responses": errors, "operations": compact}


def build_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Factor public responses and keep their referenced schemas and headers."""
    paths = document.get("paths")
    components = document.get("components", {})
    if not isinstance(components, dict):
        raise ValueError("OpenAPI document components must be an object")
    source_components = {group: components.get(group, {}) for group in SUPPORTED_COMPONENT_GROUPS}
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document must contain a paths object")
    for group, definitions in source_components.items():
        if not isinstance(definitions, dict):
            raise ValueError(f"OpenAPI document components.{group} must be an object")

    operations: dict[str, Any] = {}
    selected_names: dict[str, set[str]] = {group: set() for group in source_components}
    pending: list[tuple[str, str]] = []
    for path, path_item in paths.items():
        if not isinstance(path, str) or not path.startswith(API_PATH_PREFIX):
            continue
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if not isinstance(method, str) or method.lower() not in HTTP_METHODS:
                continue
            if not isinstance(operation, dict):
                raise ValueError(f"OpenAPI operation {method.upper()} {path} must be an object")
            if "responses" not in operation:
                raise ValueError(f"OpenAPI operation {method.upper()} {path} has no responses")
            responses = operation["responses"]
            command_path = re.sub(r"\{[^{}]+\}", "{id}", path)
            key = f"{method.upper()} {command_path}"
            if key in operations:
                raise ValueError(f"OpenAPI response paths overlap at {key}")
            operations[key] = responses
            pending.extend(_component_reference(reference) for reference in _references(responses))

    selected_components: dict[str, dict[str, Any]] = {group: {} for group in source_components}
    while pending:
        group, name = pending.pop()
        if name in selected_names[group]:
            continue
        definitions = source_components[group]
        reference = f"{COMPONENT_REF_PREFIX}{group}/{name}"
        if name not in definitions:
            raise ValueError(f"response contract reference {reference!r} does not exist")
        selected_names[group].add(name)
        definition = definitions[name]
        selected_components[group][name] = definition
        pending.extend(
            _component_reference(child_reference) for child_reference in _references(definition)
        )

    return {**_factor_responses(operations), "components": selected_components}


def render_contract(document: dict[str, Any]) -> str:
    """Render the generated Python assignment block."""
    rendered_json = json.dumps(
        build_contract(document),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f'CLIENT_RESPONSE_CONTRACT = json.loads(\n    r"""\n{rendered_json}\n"""  # noqa: E501\n)\n'
    )


def _replace_generated_block(source: str, block: str) -> str:
    if source.count(BEGIN_MARKER) != 1 or source.count(END_MARKER) != 1:
        raise ValueError("client must contain one generated response contract marker pair")
    begin = source.index(BEGIN_MARKER)
    body_start = begin + len(BEGIN_MARKER)
    end = source.index(END_MARKER)
    if end < body_start:
        raise ValueError("generated response contract markers are out of order")
    return source[:body_start] + "\n" + block + source[end:]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the generated response contract is stale",
    )
    arguments = parser.parse_args()

    try:
        document = json.loads(OPENAPI.read_text(encoding="utf-8"))
        source = CLIENT.read_text(encoding="utf-8")
        block = render_contract(document)
        desired = _replace_generated_block(source, block)
        if arguments.check:
            if desired != source:
                print(
                    "Generated client response contract is stale. "
                    "Run 'pdm run openapi' to regenerate it.",
                    file=sys.stderr,
                )
                return 1
            return 0
        CLIENT.write_text(desired, encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Could not generate client response contract: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
