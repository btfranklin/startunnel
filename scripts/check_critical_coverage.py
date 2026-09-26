"""Enforce complete line and branch coverage for critical protocol functions."""

from __future__ import annotations

import argparse
import ast
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class CriticalTarget:
    path: str
    functions: tuple[str, ...]


CRITICAL_TARGETS = (
    CriticalTarget("src/agents/services.py", ("parse_key", "authenticate_key")),
    CriticalTarget("src/api/auth.py", ("authenticate",)),
    CriticalTarget(
        "src/tunnels/codec.py",
        (
            "_checksum",
            "encode_token",
            "parse_address",
            "canonical_address",
            "display_address",
            "address_transcription",
            "address_digest",
            "derive_token",
        ),
    ),
    CriticalTarget(
        "src/tunnels/access.py",
        ("credential_is_active", "resolve_tunnel", "cycle_is_readable"),
    ),
    CriticalTarget(
        "src/tunnels/services.py",
        (
            "create_tunnel",
            "post_reply",
            "read_tree",
            "get_message",
            "get_branch",
            "load_branch",
            "list_replies",
            "close_cycle",
            "_close_locked_cycle",
        ),
    ),
    CriticalTarget("src/tunnels/retention.py", ("delete_due_cycle_content",)),
    CriticalTarget("src/api/rate_limits.py", ("_consume",)),
    CriticalTarget("src/tunnels/maintenance.py", ("cleanup_once",)),
)


def _line_range(source_path: Path, function_name: str) -> range:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Expected one function named {function_name!r} in {source_path}; found {len(matches)}."
        )
    node = matches[0]
    if node.end_lineno is None:
        raise ValueError(f"Cannot determine the end of {function_name!r} in {source_path}.")
    return range(node.lineno, node.end_lineno + 1)


def _covered_lines(file_report: dict[str, Any]) -> set[int]:
    return {int(line) for line in file_report.get("executed_lines", [])}


def _missing_lines(file_report: dict[str, Any]) -> set[int]:
    return {int(line) for line in file_report.get("missing_lines", [])}


def _missing_branch_sources(file_report: dict[str, Any]) -> set[int]:
    return {int(branch[0]) for branch in file_report.get("missing_branches", [])}


def find_gaps(report: dict[str, Any], root: Path = ROOT) -> list[str]:
    """Return human-readable gaps in the authoritative critical target set."""

    files = report.get("files", {})
    gaps: list[str] = []
    for target in CRITICAL_TARGETS:
        file_report = files.get(target.path)
        if file_report is None:
            gaps.append(f"{target.path}: file is absent from the coverage report")
            continue
        executed = _covered_lines(file_report)
        missing = _missing_lines(file_report)
        missing_branch_sources = _missing_branch_sources(file_report)
        for function_name in target.functions:
            lines = set(_line_range(root / target.path, function_name))
            relevant_missing = sorted(lines & missing)
            relevant_branch_sources = sorted(lines & missing_branch_sources)
            if not lines & executed:
                gaps.append(f"{target.path}:{function_name}: function was not executed")
            if relevant_missing:
                gaps.append(
                    f"{target.path}:{function_name}: missing lines "
                    + ", ".join(str(line) for line in relevant_missing)
                )
            if relevant_branch_sources:
                gaps.append(
                    f"{target.path}:{function_name}: missing branch from lines "
                    + ", ".join(str(line) for line in relevant_branch_sources)
                )
    return gaps


def check_report(report_path: Path, root: Path = ROOT) -> list[str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("Coverage JSON must contain an object.")
    return find_gaps(report, root)


def _print_gaps(gaps: Iterable[str]) -> None:
    for gap in gaps:
        print(f"- {gap}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", nargs="?", default="coverage.json")
    args = parser.parse_args()
    report_path = ROOT / args.report
    if not report_path.is_file():
        print(f"Coverage report does not exist: {report_path}")
        print("Recovery: run `pdm run check` to create and inspect coverage.json.")
        return 1
    gaps = check_report(report_path)
    if gaps:
        print("Critical protocol coverage is incomplete:")
        _print_gaps(gaps)
        print("Source of truth: docs/testing.md#coverage")
        print("Recovery: add behavior tests for the named functions, then run `pdm run check`.")
        return 1
    print("Critical protocol functions have complete line and branch coverage.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
