"""Put release metadata in the standalone client during an image build."""

from __future__ import annotations

import argparse
from pathlib import Path


def stamp_client(path: Path, version: str, source_revision: str) -> None:
    """Replace the one client build literal without executing the client."""

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line.startswith("CLIENT_BUILD = ")]
    if len(positions) != 1:
        raise ValueError("Expected one CLIENT_BUILD assignment in the client.")
    metadata = {"version": version, "source_revision": source_revision or None}
    lines[positions[0]] = f"CLIENT_BUILD = {metadata!r}\n"
    source = "".join(lines)
    compile(source, str(path), "exec")
    path.write_text(source, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-revision", default="")
    arguments = parser.parse_args()
    stamp_client(arguments.path, arguments.version, arguments.source_revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
