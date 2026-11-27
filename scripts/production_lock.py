"""Serialize operations on one installed production secret directory."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .start_production import production_secret_directory
else:
    from start_production import production_secret_directory

LOCK_DESCRIPTOR = "STARTUNNEL_PRODUCTION_LOCK_FD"


class ProductionLockBusy(ValueError):
    """Another operation holds the installed instance lock."""


@contextmanager
def production_lock(directory: Path) -> Iterator[int]:
    directory = directory.resolve()
    path = directory.with_name(f".{directory.name}.startunnel.lock")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "rb+") as lock:
        inherited = os.environ.get(LOCK_DESCRIPTOR)
        if inherited is not None:
            parent = int(inherited)
            if os.path.samestat(os.fstat(parent), os.fstat(descriptor)):
                fcntl.flock(parent, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield parent
                return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProductionLockBusy(
                "Another StarTunnel production operation holds the instance lock."
            ) from error
        yield lock.fileno()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    command = arguments.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("Supply the command to run under the instance lock.")
    try:
        with production_lock(production_secret_directory(root=Path.cwd())) as descriptor:
            environment = os.environ | {LOCK_DESCRIPTOR: str(descriptor)}
            return subprocess.run(
                command, env=environment, pass_fds=(descriptor,), check=False
            ).returncode
    except (OSError, ValueError) as error:
        print(error)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
