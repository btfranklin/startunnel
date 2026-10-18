"""Check CLI examples and local Markdown links without fixed document names."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
from cli.star_tunnel import CliError, build_parser
from markdown_it import MarkdownIt
from scripts import check_docs

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "cli/star_tunnel.py"
ENV_NAME = re.compile(r"STARTUNNEL_[A-Z][A-Z0-9_]*")
MARKDOWN = MarkdownIt("commonmark").enable("table")


def _markdown_files() -> list[Path]:
    paths = list(ROOT.glob("*.md"))
    for directory in ("docs", "cli", "examples", "website"):
        paths.extend((ROOT / directory).rglob("*.md"))
    return sorted(set(paths))


def _cli_doc_errors(markdown: str, known_environment: set[str]) -> list[str]:
    errors = []
    parser = build_parser()
    for fence in MARKDOWN.parse(markdown):
        if fence.type != "fence" or fence.map is None:
            continue
        commands: list[tuple[int, list[str]]] = []
        for offset, line in enumerate(fence.content.splitlines(), start=1):
            if "star_tunnel.py" not in line:
                continue
            words = shlex.split(line)
            for index, word in enumerate(words):
                if Path(word).name != "star_tunnel.py" or index == 0:
                    continue
                if words[index - 1] not in {"python", "python3"}:
                    continue
                arguments = []
                for value in words[index + 1 :]:
                    if value.startswith("<") or value == "|":
                        break
                    arguments.append(value)
                commands.append((fence.map[0] + offset, arguments))
        for line_number, arguments in commands:
            try:
                parser.parse_args(arguments)
            except CliError:
                errors.append(f"line {line_number}: invalid CLI arguments: {arguments!r}")
            except SystemExit as error:
                if error.code != 0:
                    errors.append(f"line {line_number}: invalid CLI arguments: {arguments!r}")
        if commands:
            for name in sorted(set(ENV_NAME.findall(fence.content)) - known_environment):
                errors.append(f"unknown CLI environment name: {name}")
    return errors


def test_markdown_cli_examples_match_the_client() -> None:
    known_environment = set(ENV_NAME.findall(CLI.read_text(encoding="utf-8")))
    errors = [
        f"{path.relative_to(ROOT)}: {error}"
        for path in _markdown_files()
        for error in _cli_doc_errors(path.read_text(encoding="utf-8"), known_environment)
    ]
    assert not errors, "\n".join(errors)


def test_cli_documentation_check_rejects_unknown_flags_and_environment_names() -> None:
    markdown = """```shell
export STARTUNNEL_URL=http://example.test
python3 star_tunnel.py create --text hello
```"""

    errors = _cli_doc_errors(markdown, {"STARTUNNEL_BASE_URL"})

    assert len(errors) == 2
    assert "invalid CLI arguments" in errors[0]
    assert "STARTUNNEL_URL" in errors[1]


def test_cli_documentation_check_accepts_separate_server_configuration() -> None:
    markdown = """Set the server's STARTUNNEL_DOMAIN before its update.

```shell
export STARTUNNEL_DOMAIN=tunnel.example
```

Then check the client:

```shell
export STARTUNNEL_BASE_URL=https://tunnel.example
python3 star_tunnel.py me
```"""

    assert _cli_doc_errors(markdown, {"STARTUNNEL_BASE_URL"}) == []


def test_markdown_link_checker_rejects_missing_cross_file_heading(tmp_path: Path) -> None:
    (tmp_path / "source.md").write_text("[Go](target.md#missing-heading)\n", encoding="utf-8")
    (tmp_path / "target.md").write_text("# Present heading\n", encoding="utf-8")

    result = subprocess.run(
        ["linkbust", str(tmp_path), "--no-color"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "#missing-heading" in result.stdout + result.stderr


@pytest.mark.parametrize("route", ["/app/audit/", "/app/learn/", "/app/admins/access/"])
def test_documentation_links_use_django_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text(f"[Page]({route})\n")
    assert check_docs.check_links() == []


def test_documentation_links_report_unknown_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(check_docs, "ROOT", tmp_path)
    (tmp_path / "README.md").write_text("[Page](/app/unknown-page/)\n")
    failures = check_docs.check_links()
    assert len(failures) == 1
    assert "does not resolve in Django" in failures[0].detail
