"""Private reports do not affect repository Markdown link checks."""

from __future__ import annotations

from pathlib import Path

from scripts.check_markdown_links import check


def test_source_export_checks_document_links_and_omits_report_copies(tmp_path: Path) -> None:
    source = tmp_path / "README.md"
    source.write_text("# Guide\n\n[Missing](missing.md)\n", encoding="utf-8")
    reports = tmp_path / "artifacts"
    reports.mkdir()
    (reports / "review.md").write_text("[Private](absent.md)\n", encoding="utf-8")

    assert check(tmp_path) == 1

    source.write_text("# Guide\n\n[Details](details.md)\n", encoding="utf-8")
    (tmp_path / "details.md").write_text("# Details\n", encoding="utf-8")

    assert check(tmp_path) == 0
