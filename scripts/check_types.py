"""Check every Python source file in its supported import context."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from scripts.check_secrets import candidate_files

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True, slots=True)
class TypeLane:
    name: str
    files: tuple[Path, ...]
    directory: Path
    standalone: bool = False
    python_version: str | None = None


def source_lanes(root: Path) -> tuple[TypeLane, ...]:
    """Include tracked and nonignored Python, including new files and migrations."""
    application: list[Path] = []
    reference: list[Path] = []
    sdk: list[Path] = []
    minimum: list[Path] = []
    for path in sorted(candidate_files(root)):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(root)
        if relative.is_relative_to("cli") or relative == Path("tests/cli_standalone.py"):
            minimum.append(path)
        elif relative.is_relative_to("examples/python"):
            reference.append(path)
        elif relative.is_relative_to("examples/openai_agents"):
            sdk.append(path)
        else:
            application.append(path)
    return (
        TypeLane("Application, tests, tools, website, and migrations", tuple(application), root),
        TypeLane("Standalone Python examples", tuple(reference), root / "examples/python", True),
        TypeLane("Standalone OpenAI SDK example", tuple(sdk), root, True),
        TypeLane(
            "Python 3.10 standalone client and smoke tests", tuple(minimum), root, True, "3.10"
        ),
    )


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Type-check configuration must contain a table.")
    result: dict[str, object] = {}
    for key, item in cast(Mapping[object, object], value).items():
        if not isinstance(key, str):
            raise ValueError("Type-check configuration keys must be strings.")
        result[key] = item
    return result


def _literal(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int)):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_literal(item) for item in cast(list[object], value)) + "]"
    raise ValueError("Unsupported type-check configuration value.")


def standalone_config(project_file: Path) -> str:
    """Retain all strict settings without the Django import path or plugin."""
    document: object = tomllib.loads(project_file.read_text(encoding="utf-8"))
    tools = _mapping(_mapping(document)["tool"])
    settings = _mapping(tools["mypy"])
    settings.pop("mypy_path", None)
    overrides = settings.pop("overrides", [])
    plugins = settings.get("plugins", [])
    if not isinstance(plugins, list):
        raise ValueError("Type-check plugins must be a list.")
    settings["plugins"] = [
        plugin for plugin in cast(list[object], plugins) if plugin != "mypy_django_plugin.main"
    ]
    lines = ["[tool.mypy]", *(f"{key} = {_literal(value)}" for key, value in settings.items())]
    if not isinstance(overrides, list):
        raise ValueError("Type-check overrides must be a list.")
    for override in cast(list[object], overrides):
        lines.extend(["", "[[tool.mypy.overrides]]"])
        lines.extend(f"{key} = {_literal(value)}" for key, value in _mapping(override).items())
    lines.extend(["", "[tool.pydantic-mypy]"])
    lines.extend(
        f"{key} = {_literal(value)}" for key, value in _mapping(tools["pydantic-mypy"]).items()
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    lanes = source_lanes(ROOT)
    with tempfile.TemporaryDirectory(prefix="startunnel-types-") as temporary:
        standalone = Path(temporary) / "pyproject.toml"
        standalone.write_text(standalone_config(ROOT / "pyproject.toml"), encoding="utf-8")
        for lane in lanes:
            if not lane.files:
                continue
            environment = os.environ.copy()
            # Resolve the SDK's agents package separately from the Django agents app.
            environment["PYTHONPATH"] = os.pathsep.join(
                [str(ROOT)] if lane.standalone else [str(ROOT), str(ROOT / "src")]
            )
            environment["MYPYPATH"] = ""
            command = [
                sys.executable,
                "-m",
                "mypy",
                "--config-file",
                str(standalone if lane.standalone else ROOT / "pyproject.toml"),
            ]
            if not lane.standalone:
                command.append("--explicit-package-bases")
            if lane.python_version is not None:
                command.extend(["--python-version", lane.python_version])
            command.extend(str(path.relative_to(lane.directory)) for path in lane.files)
            print(f"{lane.name}: {len(lane.files)} Python files", flush=True)
            result = subprocess.run(command, cwd=lane.directory, env=environment, check=False)
            if result.returncode:
                return result.returncode
    print(f"All {sum(len(lane.files) for lane in lanes)} Python source files passed type checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
