"""Keep new Python source and standalone import contexts inside the type gate."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from scripts.check_types import source_lanes, standalone_config


def test_source_export_checks_new_files_and_migrations(tmp_path: Path) -> None:
    expected = {
        "src/accounts/migrations/0001_initial.py",
        "tests/fixtures/sample.py",
        "new_tools/utility.py",
        "manage.py",
        "examples/python/example.py",
        "examples/openai_agents/example.py",
        "cli/client.py",
        "tests/cli_standalone.py",
    }
    for name in expected | {"artifacts/report.py", ".venv/vendor.py"}:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    lanes = source_lanes(tmp_path)
    checked = [path.relative_to(tmp_path).as_posix() for lane in lanes for path in lane.files]
    assert set(checked) == expected
    assert len(checked) == len(set(checked))
    assert not lanes[0].standalone
    assert lanes[1].directory == tmp_path / "examples/python"
    assert lanes[2].standalone
    assert lanes[3].python_version == "3.10"


def test_standalone_context_keeps_strict_settings(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(
        '[tool.mypy]\npython_version = "3.15"\nstrict = true\n'
        "disallow_any_explicit = true\ndisallow_any_unimported = true\n"
        'plugins = ["mypy_django_plugin.main", "pydantic.mypy"]\nmypy_path = "src"\n'
        "[tool.pydantic-mypy]\ninit_typed = true\ninit_forbid_extra = true\n",
        encoding="utf-8",
    )
    document = tomllib.loads(standalone_config(project))
    settings = document["tool"]["mypy"]
    assert settings["strict"]
    assert settings["disallow_any_explicit"]
    assert settings["disallow_any_unimported"]
    assert settings["plugins"] == ["pydantic.mypy"]
    assert "mypy_path" not in settings
    assert document["tool"]["pydantic-mypy"]["init_typed"]


def test_strict_gate_rejects_unchecked_values_and_factory_arguments(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config = tmp_path / "pyproject.toml"
    config.write_text(standalone_config(root / "pyproject.toml"), encoding="utf-8")
    probe = tmp_path / "unsafe.py"
    probe.write_text(
        "from typing import Any, Protocol\n"
        "import missing_vendor  # type: ignore[import-not-found]\n"
        "class Factory(Protocol):\n"
        "    def __call__(self, *, name: str) -> int: ...\n"
        "def use(factory: Factory, vendor: missing_vendor.Client) -> None:\n"
        "    factory(nmae='incorrect keyword')\n"
        "    values: list[int] = ['wrong item type']\n"
        "    unchecked: Any = 1\n"
        "    bare: list = []\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONPATH"] = ""
    environment["MYPYPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "--config-file", str(config), str(probe)],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    for code in ("explicit-any", "no-any-unimported", "type-arg", "call-arg", "list-item"):
        assert f"[{code}]" in result.stdout, result.stdout
