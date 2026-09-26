"""Render a fixed set of repository-owned public Markdown documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import cast

from django.conf import settings
from django.utils.html import format_html
from django.utils.safestring import SafeString, mark_safe
from markdown_it import MarkdownIt
from mdit_py_plugins.anchors import anchors_plugin
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import TextLexer, get_lexer_for_filename
from pygments.util import ClassNotFound


@dataclass(frozen=True, slots=True)
class DocumentationPage:
    slug: str
    title: str
    source: str


DOCUMENTATION_PAGES = (
    DocumentationPage("index", "Documentation", "index.md"),
    DocumentationPage("agent-quickstart", "Agent quickstart", "agent-quickstart.md"),
    DocumentationPage("tutorial", "Guided two-agent tutorial", "tutorial.md"),
    DocumentationPage("concepts", "Concepts", "concepts.md"),
    DocumentationPage("authentication", "Authentication", "authentication.md"),
    DocumentationPage("instance-tunnels", "Instance tunnels", "instance-tunnels.md"),
    DocumentationPage(
        "rotation-and-history",
        "Address rotation and history",
        "rotation-and-history.md",
    ),
    DocumentationPage("delivery", "Delivery and recovery", "delivery.md"),
    DocumentationPage("errors", "Errors", "errors.md"),
    DocumentationPage("limits", "Limits", "limits.md"),
    DocumentationPage("security", "Security", "security.md"),
    DocumentationPage("examples", "Examples", "examples.md"),
    DocumentationPage("local-development", "Local development", "local-development.md"),
)
DOCUMENTATION_BY_SLUG = {page.slug: page for page in DOCUMENTATION_PAGES}

_EXAMPLE_DIRECTIVE = re.compile(
    r'^\{\{\s*example\s+"(?P<path>[^"]+)"\s+region="(?P<region>[^"]+)"\s*\}\}$',
    re.MULTILINE,
)


def _example_region(relative_path: str, region: str) -> str:
    examples_root = (Path(settings.PROJECT_ROOT) / "examples").resolve()
    source_path = (examples_root / relative_path).resolve()
    if not source_path.is_relative_to(examples_root) or not source_path.is_file():
        raise ValueError(f"The documentation example does not exist: {relative_path}")

    start_marker = f"# region {region}"
    end_marker = f"# endregion {region}"
    lines = source_path.read_text(encoding="utf-8").splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == start_marker) + 1
        end = next(
            index
            for index, line in enumerate(lines[start:], start=start)
            if line.strip() == end_marker
        )
    except StopIteration as error:
        raise ValueError(
            f"The documentation example region does not exist: {relative_path}#{region}"
        ) from error
    language = source_path.suffix.removeprefix(".") or "text"
    return f"```{language}\n" + "\n".join(lines[start:end]) + "\n```"


def _expand_examples(markdown: str) -> str:
    return _EXAMPLE_DIRECTIVE.sub(
        lambda match: _example_region(match.group("path"), match.group("region")), markdown
    )


def _highlight(code: str, language: str, _attributes: str) -> str:
    try:
        lexer = get_lexer_for_filename(f"example.{language}") if language else TextLexer()
    except ClassNotFound:
        lexer = TextLexer()
    return str(highlight(code, lexer, HtmlFormatter(nowrap=True)))


@lru_cache(maxsize=len(DOCUMENTATION_PAGES))
def _render_cached(source_path: str, modified_ns: int) -> SafeString:
    del modified_ns
    markdown = _expand_examples(Path(source_path).read_text(encoding="utf-8"))
    renderer = MarkdownIt(
        "commonmark",
        {"html": False, "linkify": True, "typographer": True, "highlight": _highlight},
    ).enable("table")
    renderer.use(anchors_plugin, max_level=3)
    rendered = renderer.render(markdown)
    # Markdown raw HTML is disabled. Example source passes through Pygments escaping.
    return cast(SafeString, mark_safe(rendered))  # nosec B308, B703


def render_documentation(slug: str) -> tuple[DocumentationPage, SafeString]:
    page = DOCUMENTATION_BY_SLUG[slug]
    source_path = Path(settings.PROJECT_ROOT) / "docs" / "user" / page.source
    if not source_path.is_file():
        return page, format_html("<p>{} is not available.</p>", page.title)
    return page, _render_cached(str(source_path), source_path.stat().st_mtime_ns)
