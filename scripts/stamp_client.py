"""Put release metadata in the standalone client during an image build."""

from __future__ import annotations

import argparse
from pathlib import Path


def stamp_build(path: Path, name: str, version: str, source_revision: str) -> None:
    """Replace one build literal without executing the source file."""

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line.startswith(name + " = ")]
    if len(positions) != 1:
        raise ValueError(f"Expected one {name} assignment in the source file.")
    metadata = {"version": version, "source_revision": source_revision or None}
    lines[positions[0]] = f"{name} = {metadata!r}\n"
    source = "".join(lines)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")


def stamp_client(path: Path, version: str, source_revision: str) -> None:
    """Stamp the standalone client used by source and image builds."""

    stamp_build(path, "CLIENT_BUILD", version, source_revision)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", default="")
    parser.add_argument("--server-build", type=Path)
    arguments = parser.parse_args()
    stamp_client(arguments.path, arguments.version, arguments.source_revision)
    if arguments.server_build is not None:
        stamp_build(arguments.server_build, "BUILD", arguments.version, arguments.source_revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
