"""Validate documentation links, route ownership, and included example regions."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = (ROOT / "examples").resolve()
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
EXAMPLE_INCLUDE = re.compile(r'\{\{\s*example\s+"([^"]+)"\s+region="([A-Za-z0-9_-]+)"\s*\}\}')
PUBLIC_ROUTES = {
    "/",
    "/api/docs/",
    "/api/v1/openapi.json",
    "/docs/",
    "/docs/agent-quickstart/",
    "/docs/tutorial/",
    "/docs/concepts/",
    "/docs/authentication/",
    "/docs/instance-tunnels/",
    "/docs/delivery/",
    "/docs/errors/",
    "/docs/limits/",
    "/docs/security/",
    "/docs/examples/",
    "/docs/rotation-and-history/",
    "/docs/local-development/",
    "/downloads/star_tunnel.py",
    "/app/",
    "/app/account/",
    "/app/admins/",
    "/app/agents/",
    "/app/tunnels/",
}


@dataclass(frozen=True, slots=True)
class Failure:
    path: Path
    line: int
    detail: str
    recovery: str

    def render(self) -> str:
        relative = self.path.relative_to(ROOT)
        return (
            "Rule: Documentation links and example includes resolve to an owned source.\n"
            "Source of truth: docs/README.md\n"
            f"Problem: {relative}:{self.line}: {self.detail}\n"
            f"Recovery: {self.recovery}"
        )


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _strip_destination(destination: str) -> str:
    destination = destination.strip()
    if destination.startswith("<") and destination.endswith(">"):
        destination = destination[1:-1]
    return destination.split(maxsplit=1)[0]


def check_links() -> list[Failure]:
    failures: list[Failure] = []
    markdown_files = (
        sorted(
            path
            for root in [ROOT, ROOT / "docs", ROOT / "examples", ROOT / "cli", ROOT / "website"]
            for path in root.glob("*.md")
            if path.is_file()
        )
        + sorted((ROOT / "docs").rglob("*.md"))
        + sorted((ROOT / "examples").rglob("*.md"))
        + sorted((ROOT / "cli").rglob("*.md"))
        + sorted((ROOT / "website").rglob("*.md"))
    )
    for path in sorted(set(markdown_files)):
        text = path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            destination = _strip_destination(match.group(1))
            parsed = urlsplit(destination)
            if parsed.scheme in {"http", "https", "mailto"} or destination.startswith("#"):
                continue
            decoded_path = unquote(parsed.path)
            if decoded_path.startswith("/"):
                if decoded_path not in PUBLIC_ROUTES:
                    failures.append(
                        Failure(
                            path,
                            _line_number(text, match.start()),
                            f"Public route {decoded_path!r} is not in the fixed route manifest.",
                            (
                                "Add the route to the Django documentation manifest and "
                                "PUBLIC_ROUTES, or correct the link."
                            ),
                        )
                    )
                continue
            target = (path.parent / decoded_path).resolve()
            if not target.exists():
                failures.append(
                    Failure(
                        path,
                        _line_number(text, match.start()),
                        f"Local link target {destination!r} does not exist.",
                        (
                            f"Correct the link in {path.relative_to(ROOT)} or add "
                            f"{target.relative_to(ROOT)}."
                        ),
                    )
                )
    return failures


def _region_exists(path: Path, region: str) -> bool:
    text = path.read_text(encoding="utf-8")
    start = re.compile(rf"^\s*(?:#|//)\s*region\s+{re.escape(region)}\s*$", re.MULTILINE)
    end = re.compile(rf"^\s*(?:#|//)\s*endregion\s+{re.escape(region)}\s*$", re.MULTILINE)
    start_match = start.search(text)
    end_match = end.search(text)
    return bool(start_match and end_match and start_match.end() < end_match.start())


def check_example_includes() -> list[Failure]:
    failures: list[Failure] = []
    for path in sorted((ROOT / "docs/user").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for match in EXAMPLE_INCLUDE.finditer(text):
            relative, region = match.groups()
            target = (EXAMPLES / relative).resolve()
            if not target.is_relative_to(EXAMPLES):
                failures.append(
                    Failure(
                        path,
                        _line_number(text, match.start()),
                        "An example include leaves the examples directory.",
                        f"Use a path below examples/ in {path.relative_to(ROOT)}.",
                    )
                )
            elif not target.is_file():
                failures.append(
                    Failure(
                        path,
                        _line_number(text, match.start()),
                        f"Included example {relative!r} does not exist.",
                        (
                            f"Add examples/{relative} or correct the include in "
                            f"{path.relative_to(ROOT)}."
                        ),
                    )
                )
            elif not _region_exists(target, region):
                failures.append(
                    Failure(
                        path,
                        _line_number(text, match.start()),
                        f"Example region {region!r} does not exist in examples/{relative}.",
                        (
                            f"Add matching region markers to examples/{relative} or "
                            "correct the include."
                        ),
                    )
                )
    return failures


def main() -> int:
    failures = [
        *check_links(),
        *check_example_includes(),
    ]
    if failures:
        print("Documentation check failed.\n", file=sys.stderr)
        print("\n\n".join(failure.render() for failure in failures), file=sys.stderr)
        return 1
    print("Documentation check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
