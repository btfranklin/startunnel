"""Validate ownership of public documentation routes."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import django
from django.urls import Resolver404, resolve

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")


@dataclass(frozen=True, slots=True)
class Failure:
    path: Path
    line: int
    detail: str
    recovery: str

    def render(self) -> str:
        relative = self.path.relative_to(ROOT)
        return (
            "Rule: Documentation routes resolve to an owned view.\n"
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
                try:
                    resolve(decoded_path)
                except Resolver404:
                    failures.append(
                        Failure(
                            path,
                            _line_number(text, match.start()),
                            f"Public route {decoded_path!r} does not resolve in Django.",
                            ("Add the route to the Django URL configuration, or correct the link."),
                        )
                    )
                continue
    return failures


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "startunnel.settings.development")
    django.setup()
    failures = check_links()
    if failures:
        print("Documentation check failed.\n", file=sys.stderr)
        print("\n\n".join(failure.render() for failure in failures), file=sys.stderr)
        return 1
    print("Documentation check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
