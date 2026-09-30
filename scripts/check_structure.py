"""Check architectural and repository rules that normal linters cannot express."""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


@dataclass(frozen=True, slots=True)
class Failure:
    rule: str
    owner: str
    recovery: str
    detail: str

    def render(self) -> str:
        return (
            f"Rule: {self.rule}\n"
            f"Source of truth: {self.owner}\n"
            f"Problem: {self.detail}\n"
            f"Recovery: {self.recovery}"
        )


def _python_files() -> list[Path]:
    return sorted(SOURCE.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def check_import_directions() -> list[Failure]:
    failures: list[Failure] = []
    transport_modules = ("api", "site_app", "ninja", "django.http", "django.shortcuts")
    service_forbidden = (*transport_modules, "django.template", "openai")
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        imports = _imports(path)
        if path.name == "models.py":
            forbidden = sorted(name for name in imports if name.startswith(transport_modules))
            if forbidden:
                failures.append(
                    Failure(
                        "Models do not import transport modules.",
                        "docs/architecture.md",
                        f"Remove {', '.join(forbidden)} from {relative}.",
                        "A model imports an API or browser-layer module.",
                    )
                )
        if path.name == "services.py" or (
            path.parent.name == "tunnels"
            and path.name
            in {
                "access.py",
                "idempotency.py",
                "maintenance.py",
            }
        ):
            forbidden = sorted(name for name in imports if name.startswith(service_forbidden))
            if forbidden:
                failures.append(
                    Failure(
                        "Domain services do not import HTTP, template, or OpenAI code.",
                        "docs/architecture.md",
                        f"Move the transport dependency out of {relative}: {', '.join(forbidden)}.",
                        "A domain service depends on a transport or model-provider layer.",
                    )
                )
    return failures


def check_sensitive_route_fields() -> list[Failure]:
    failures: list[Failure] = []
    for path in [SOURCE / "api/router.py", *SOURCE.rglob("urls.py")]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        match = re.search(
            r"\{(?:address|message|cycle|cursor|receipt(?:_handle)?)\}"
            r"|<(?:str|path):(?:address|message|cycle|cursor|receipt)",
            text,
        )
        if match:
            failures.append(
                Failure(
                    "Tree bearer values and identifiers do not appear in URL paths.",
                    "docs/security.md",
                    f"Move the sensitive value into a request body in {path.relative_to(ROOT)}.",
                    f"A route contains the sensitive path field {match.group(0)!r}.",
                )
            )
    return failures


def check_sensitive_logging() -> list[Failure]:
    sensitive = {
        "address",
        "cursor",
        "receipt",
        "payload",
        "content",
        "password",
        "key",
        "token",
        "secret",
    }
    failures: list[Failure] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
                continue
            for argument in [*node.args[1:], *(keyword.value for keyword in node.keywords)]:
                names = {
                    child.id.lower() for child in ast.walk(argument) if isinstance(child, ast.Name)
                }
                names.update(
                    child.attr.lower()
                    for child in ast.walk(argument)
                    if isinstance(child, ast.Attribute)
                )
                matched = sorted(name for name in names if any(term in name for term in sensitive))
                if matched:
                    failures.append(
                        Failure(
                            "Logs do not contain secrets, addresses, cursors, or message content.",
                            "docs/security.md",
                            (
                                "Remove the sensitive logging argument from "
                                f"{path.relative_to(ROOT)}:{node.lineno}."
                            ),
                            f"A logging call uses sensitive value name(s): {', '.join(matched)}.",
                        )
                    )
    return failures


def check_transport_logging() -> list[Failure]:
    failures: list[Failure] = []
    for path in [ROOT / "Dockerfile", ROOT / "compose.yaml", ROOT / "compose.dev.yaml"]:
        text = path.read_text(encoding="utf-8")
        if text.count("uvicorn") != text.count("--no-access-log"):
            failures.append(
                Failure(
                    "Transport access logs do not record browser bearer-token URLs.",
                    "docs/security.md",
                    f"Add `--no-access-log` to every Uvicorn command in {path.relative_to(ROOT)}.",
                    "Uvicorn can log an OAuth code, state, or invitation token from a URL.",
                )
            )
    caddyfile = ROOT / "docker/caddy/Caddyfile"
    caddy_text = caddyfile.read_text(encoding="utf-8")
    if caddy_text.count("request>uri delete") < 2:
        failures.append(
            Failure(
                "Edge logs exclude complete request URIs.",
                "docs/security.md",
                "Filter `request>uri` from both Caddy runtime and access logs.",
                "Caddy can record an OAuth code, state, or invitation token from a URL.",
            )
        )
    if caddy_text.count("request>headers delete") < 2:
        failures.append(
            Failure(
                "Edge logs exclude request headers.",
                "docs/security.md",
                "Filter `request>headers` from both Caddy loggers.",
                "Caddy can record authorization, retry, CSRF, metrics, or referrer values.",
            )
        )
    if caddy_text.count("resp_headers delete") < 2:
        failures.append(
            Failure(
                "Edge logs exclude response headers.",
                "docs/security.md",
                "Filter `resp_headers` from both Caddy loggers.",
                "Caddy can record a redirect Location that contains a browser bearer token.",
            )
        )
    return failures


def check_settings_documentation() -> list[Failure]:
    declared = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = declared + "\n" + (ROOT / "docs/operations.md").read_text(encoding="utf-8")
    documented += "\n" + (ROOT / "docs/testing.md").read_text(encoding="utf-8")
    pattern = re.compile(r"(?:env|os\.getenv)\(\s*[\"']([A-Z][A-Z0-9_]+)[\"']")
    failures: list[Failure] = []
    for path in sorted((SOURCE / "startunnel/settings").glob("*.py")):
        for name in pattern.findall(path.read_text(encoding="utf-8")):
            if name not in documented:
                failures.append(
                    Failure(
                        "Every environment setting is documented.",
                        "docs/operations.md",
                        (
                            f"Add {name} to .env.example or the applicable test section in "
                            "docs/testing.md."
                        ),
                        f"{name} is used in {path.relative_to(ROOT)} but is not documented.",
                    )
                )
    return failures


def check_migration_roots() -> list[Failure]:
    failures: list[Failure] = []
    for model_file in sorted(SOURCE.glob("*/models.py")):
        text = model_file.read_text(encoding="utf-8")
        if "models.Model" not in text:
            continue
        migration_root = model_file.parent / "migrations"
        if not (migration_root / "__init__.py").exists() or not list(migration_root.glob("0*.py")):
            failures.append(
                Failure(
                    "Each persisted Django app has committed migrations.",
                    "docs/testing.md",
                    (
                        "Run `pdm run python manage.py makemigrations`, then inspect and "
                        "commit the result."
                    ),
                    f"{model_file.parent.name} has models but no initial migration.",
                )
            )
    return failures


def collect_failures() -> list[Failure]:
    checks = [
        check_import_directions,
        check_sensitive_route_fields,
        check_sensitive_logging,
        check_transport_logging,
        check_settings_documentation,
        check_migration_roots,
    ]
    return [failure for check in checks for failure in check()]


def main() -> int:
    failures = collect_failures()
    if failures:
        print("Repository structure check failed.\n", file=sys.stderr)
        print("\n\n".join(failure.render() for failure in failures), file=sys.stderr)
        return 1
    print("Repository structure check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
