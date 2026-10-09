"""Run the canonical fast validation gate with direct recovery commands."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class Gate:
    name: str
    command: tuple[str, ...]
    owner: str
    recovery: str
    environment: dict[str, str] | None = None


GATES = [
    Gate(
        "Ruff lint",
        ("ruff", "check", "."),
        "docs/testing.md",
        "Run `pdm run ruff check . --fix`, then inspect the changes.",
    ),
    Gate(
        "Ruff format",
        ("ruff", "format", "--check", "."),
        "docs/testing.md",
        "Run `pdm run ruff format .`, then inspect the changes.",
    ),
    Gate(
        "mypy",
        ("mypy", "src", "tests"),
        "docs/architecture.md",
        "Correct the reported types in src/ or tests/, then run `pdm run mypy src tests`.",
        {"PYTHONPATH": "src"},
    ),
    Gate(
        "djLint",
        (sys.executable, "scripts/check_templates.py"),
        "docs/design.md",
        "Run `pdm run djlint --reformat templates`, then inspect the template changes.",
    ),
    Gate(
        "Django system check",
        (sys.executable, "manage.py", "check"),
        "docs/operations.md",
        "Correct the reported Django setting or application error.",
    ),
    Gate(
        "Django deployment check",
        (
            sys.executable,
            "manage.py",
            "check",
            "--deploy",
            "--settings=startunnel.settings.production",
        ),
        "docs/operations.md",
        "Correct the reported production setting in src/startunnel/settings/production.py.",
        {
            "STARTUNNEL_SECRET_KEY": (
                "check-only-secret-that-is-long-enough-and-never-used-outside-this-process"
            ),
            "STARTUNNEL_API_KEY_PEPPER": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE",
            "STARTUNNEL_ADDRESS_SECRET": "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI",
            "STARTUNNEL_ADDRESS_DERIVATION_SECRET": ("AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM"),
            "STARTUNNEL_IDEMPOTENCY_SECRET": ("BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"),
            "STARTUNNEL_METRICS_TOKEN": "BgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgYGBgY",
            "DATABASE_URL": "postgresql://check:check@localhost/check",
            "STARTUNNEL_ALLOWED_HOSTS": "check.startunnel.test",
            "STARTUNNEL_CSRF_TRUSTED_ORIGINS": "https://check.startunnel.test",
            "STARTUNNEL_BASE_URL": "https://check.startunnel.test",
        },
    ),
    Gate(
        "Migration drift",
        (sys.executable, "manage.py", "makemigrations", "--check", "--dry-run"),
        "docs/testing.md",
        "Run `pdm run python manage.py makemigrations`, then inspect the migration.",
        {"DATABASE_URL": "sqlite:///:memory:"},
    ),
    Gate(
        "Repository structure",
        (sys.executable, "scripts/check_structure.py"),
        "docs/architecture.md",
        "Apply the recovery action printed by the structural check.",
    ),
    Gate(
        "Documentation",
        (sys.executable, "scripts/check_docs.py"),
        "docs/README.md",
        "Apply the recovery action printed by the documentation check.",
    ),
    Gate(
        "Perfect Doc structure",
        ("perfect-doc", "check", ".", "--offline", "--no-banner"),
        "docs/testing.md",
        "Install Perfect Doc, then run `pdm run docs-structure`.",
    ),
    Gate(
        "Markdown links and headings",
        ("linkbust", "--no-color"),
        "docs/README.md",
        "Correct the named local link or heading, then run `pdm run linkbust --no-color`.",
    ),
    Gate(
        "OpenAPI drift",
        (sys.executable, "scripts/check_openapi.py"),
        "docs/api.md",
        "Run `pdm run openapi`, inspect the diff, and update docs/api.md.",
    ),
    Gate(
        "Examples",
        (sys.executable, "scripts/check_examples.py"),
        "examples/README.md",
        "Correct the named example and run `pdm run python scripts/check_examples.py`.",
    ),
    Gate(
        "Secret shapes",
        (sys.executable, "scripts/check_secrets.py"),
        "docs/security.md",
        "Remove and rotate the named value, then run `pdm run python scripts/check_secrets.py`.",
    ),
    Gate(
        "npm assets",
        ("npm", "run", "check"),
        "docs/design.md",
        "Run the failing npm subcommand and correct the asset manifest or source.",
    ),
    Gate(
        "Fast automated tests",
        (
            "pytest",
            "tests/unit",
            "tests/api",
            "tests/docs",
            "tests/load",
            "tests/structure",
            "-m",
            "not postgres and not browser and not live_agents",
            "--cov=src",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=json:coverage.json",
        ),
        "docs/testing.md",
        "Run the reported pytest node ID, correct the behavior, and run `pdm run check` again.",
        # Fault-injection tests use SQLite. PostgreSQL constraints and races have a separate lane.
        {"TEST_DATABASE_URL": ""},
    ),
    Gate(
        "Critical protocol coverage",
        (sys.executable, "scripts/check_critical_coverage.py", "coverage.json"),
        "docs/testing.md",
        "Add behavior tests for the named function, then run `pdm run check` again.",
    ),
]


def main() -> int:
    for index, gate in enumerate(GATES, start=1):
        print(f"\n[{index}/{len(GATES)}] {gate.name}", flush=True)
        print("$ " + " ".join(gate.command), flush=True)
        environment = os.environ.copy()
        if gate.environment:
            environment.update(gate.environment)
        result = subprocess.run(gate.command, cwd=ROOT, env=environment, check=False)
        if result.returncode:
            print(f"\nRule: {gate.name} must pass.", file=sys.stderr)
            print(f"Source of truth: {gate.owner}", file=sys.stderr)
            print(f"Recovery: {gate.recovery}", file=sys.stderr)
            return result.returncode
    print("\nAll fast checks passed.")
    print("PostgreSQL, browser, Docker, load, and live-agent checks are separate lanes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
