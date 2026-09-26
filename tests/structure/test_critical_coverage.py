"""Tests for the focused critical protocol coverage gate."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from scripts.check_critical_coverage import CriticalTarget, find_gaps


def _function_lines(path: Path, name: str) -> tuple[list[int], list[int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name
    )
    assert node.end_lineno is not None
    lines = list(range(node.lineno, node.end_lineno + 1))
    branch_lines = [
        item.lineno
        for item in ast.walk(node)
        if isinstance(item, (ast.If, ast.Match, ast.For, ast.While, ast.Try))
    ]
    return lines, branch_lines


def test_focused_coverage_gate_detects_a_missing_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "critical.py"
    source.write_text(
        "def decide(value: bool) -> str:\n    if value:\n        return 'yes'\n    return 'no'\n",
        encoding="utf-8",
    )
    lines, branch_lines = _function_lines(source, "decide")
    report: dict[str, Any] = {
        "files": {
            "critical.py": {
                "executed_lines": lines,
                "missing_lines": [],
                "missing_branches": [[branch_lines[0], lines[-1]]],
            }
        }
    }
    monkeypatch.setattr(
        "scripts.check_critical_coverage.CRITICAL_TARGETS",
        (CriticalTarget("critical.py", ("decide",)),),
    )

    gaps = find_gaps(report, tmp_path)

    assert gaps == ["critical.py:decide: missing branch from lines 2"]


def test_focused_coverage_gate_accepts_complete_function(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "critical.py"
    source.write_text("def done() -> bool:\n    return True\n", encoding="utf-8")
    lines, _ = _function_lines(source, "done")
    report: dict[str, Any] = {
        "files": {
            "critical.py": {
                "executed_lines": lines,
                "missing_lines": [],
                "missing_branches": [],
            }
        }
    }
    monkeypatch.setattr(
        "scripts.check_critical_coverage.CRITICAL_TARGETS",
        (CriticalTarget("critical.py", ("done",)),),
    )

    assert find_gaps(report, tmp_path) == []
