"""Preview or remove disposable local release caches without removing evidence."""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING or __package__:
    from .json_data import json_object
else:
    from json_data import json_object

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIRECTORIES = {"scanners", "trivy-cache", "__pycache__"}


class CleanupTarget(TypedDict):
    path: str
    bytes: int


class CleanupResult(TypedDict):
    applied: bool
    targets: list[CleanupTarget]
    total_bytes: int


def _checked_path(path: Path, artifacts: Path) -> None:
    if not path.is_relative_to(artifacts) or ".." in path.parts:
        raise ValueError("Cleanup paths must stay inside repository artifacts.")
    for member in (artifacts, *path.relative_to(artifacts).parents, path):
        candidate = member if member.is_absolute() else artifacts / member
        if candidate.is_symlink():
            raise ValueError("Cleanup refuses symbolic links.")


def _artifact_files(artifacts: Path) -> tuple[list[Path], list[Path]]:
    caches: list[Path] = []
    files: list[Path] = []
    for current, directories, names in os.walk(artifacts, followlinks=False, onerror=_walk_error):
        directory = Path(current)
        for name in sorted(directories):
            child = directory / name
            if name in CACHE_DIRECTORIES:
                caches.append(child)
        directories[:] = sorted(name for name in directories if not (directory / name).is_symlink())
        files.extend(directory / name for name in sorted(names))
    return caches, files


def _walk_error(error: OSError) -> None:
    raise error


def _protected_paths(files: list[Path], artifacts: Path) -> set[Path]:
    protected: set[Path] = set()
    for path in files:
        if not path.name.startswith("proof-") or path.suffix != ".json":
            continue
        _checked_path(path, artifacts)
        if not path.is_file():
            raise ValueError("A retained proof manifest must be a regular file.")
        manifest = json_object(json.loads(path.read_text()))
        if "files" not in manifest:
            continue
        protected.add(path)
        for name in json_object(manifest["files"]):
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("A retained proof has an unsafe file reference.")
            referenced = path.parent / relative
            _checked_path(referenced, artifacts)
            protected.add(referenced)
    return protected


def _cache_size(path: Path, artifacts: Path, protected: set[Path]) -> int:
    _checked_path(path, artifacts)
    total = 0
    for current, directories, names in os.walk(path, followlinks=False, onerror=_walk_error):
        directory = Path(current)
        for name in directories:
            _checked_path(directory / name, artifacts)
        for name in names:
            member = directory / name
            _checked_path(member, artifacts)
            if member in protected:
                raise ValueError("A selected cache contains retained proof evidence.")
            if not member.is_file():
                raise ValueError("A selected cache contains a non-regular file.")
            if (
                name.startswith(".env")
                or name in {"credentials", "credentials.json", "config.json"}
                or member.suffix.lower() in {".key", ".pem", ".p12", ".pfx"}
                or "secrets" in member.relative_to(path).parts
            ):
                raise ValueError("A selected cache can contain private credentials.")
            if member.suffix.lower() in {".jsonl", ".sarif", ".log"} or (
                member.suffix.lower() == ".json" and name != "metadata.json"
            ):
                raise ValueError("A selected cache can contain a retained report.")
            total += member.stat().st_size
    return total


def cleanup(directory: Path = Path("artifacts"), *, apply: bool = False) -> CleanupResult:
    """Check all selected paths before removing any known cache directory."""
    artifacts = ROOT / "artifacts"
    selected = directory if directory.is_absolute() else ROOT / directory
    _checked_path(selected, artifacts)
    if not selected.exists():
        return {"applied": apply, "targets": [], "total_bytes": 0}
    if not selected.is_dir():
        raise ValueError("The cleanup location must be a directory.")
    caches, files = _artifact_files(artifacts)
    protected = _protected_paths(files, artifacts)
    candidates = sorted(
        (path for path in caches if path.is_relative_to(selected)), key=lambda path: path.parts
    )
    paths: list[Path] = []
    targets: list[CleanupTarget] = []
    for path in candidates:
        if any(path.is_relative_to(parent) for parent in paths):
            continue
        size = _cache_size(path, artifacts, protected)
        paths.append(path)
        targets.append({"path": str(path.relative_to(ROOT)), "bytes": size})
    if apply:
        for path in paths:
            _checked_path(path, artifacts)
        for path in paths:
            shutil.rmtree(path)
    return {"applied": apply, "targets": targets, "total_bytes": sum(t["bytes"] for t in targets)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("artifacts"))
    parser.add_argument(
        "--apply", action="store_true", help="Remove the previewed cache directories."
    )
    parser.add_argument("--json", action="store_true", help="Print the checked result as JSON.")
    arguments = parser.parse_args()
    try:
        result = cleanup(arguments.directory, apply=arguments.apply)
    except OSError as error:
        parser.error(f"Cannot inspect or remove local caches: {type(error).__name__}.")
    except ValueError as error:
        parser.error(str(error))
    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print("Removed caches:" if result["applied"] else "Cache cleanup preview:")
        for target in result["targets"]:
            print(f"- {target['path']}: {target['bytes']} bytes")
        print(f"Total: {len(result['targets'])} directories; {result['total_bytes']} bytes.")
        if not result["applied"]:
            print("Use --apply to remove these directories. Reports and unknown directories stay.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
