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
SCHEMA_REF_PREFIX = "#/components/schemas/"
API_PATH_PREFIX = "/api/"
HTTP_METHODS = {"get", "post"}


def _schema_name(reference: str) -> str:
    if not reference.startswith(SCHEMA_REF_PREFIX):
        raise ValueError(
            f"response contract reference {reference!r} is outside #/components/schemas/"
        )
    name = reference.removeprefix(SCHEMA_REF_PREFIX).replace("~1", "/").replace("~0", "~")
    if not name:
        raise ValueError(f"response contract reference {reference!r} has no schema name")
    return name


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


def build_contract(document: dict[str, Any]) -> dict[str, Any]:
    """Build the public GET/POST response mapping and referenced schema closure."""
    paths = document.get("paths")
    components = document.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document must contain a paths object")
    if not isinstance(schemas, dict):
        raise ValueError("OpenAPI document components.schemas must be an object")

    operations: dict[str, Any] = {}
    schema_names: set[str] = set()
    pending: list[str] = []
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
            pending.extend(_references(responses))

    response_schemas: dict[str, Any] = {}
    while pending:
        reference = pending.pop()
        name = _schema_name(reference)
        if name in schema_names:
            continue
        if name not in schemas:
            raise ValueError(f"response contract reference {reference!r} does not exist")
        schema_names.add(name)
        schema = schemas[name]
        response_schemas[name] = schema
        pending.extend(_references(schema))

    return {"operations": operations, "schemas": response_schemas}


def render_contract(document: dict[str, Any]) -> str:
    """Render the generated Python assignment block."""
    rendered_json = json.dumps(
        build_contract(document),
        ensure_ascii=False,
        indent=2,
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
