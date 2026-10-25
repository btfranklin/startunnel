"""Hold one bounded table lock in a disposable diagnostic load run."""

from __future__ import annotations

import os
import selectors
import subprocess  # nosec B404
import sys
from contextlib import AbstractContextManager
from pathlib import Path

# The child reads its connection string from its own environment. It prints no data.
LOCK_PROGRAM = """
import os,sys
import psycopg
with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=5) as connection:
    connection.execute("SET statement_timeout = '65s'")
    connection.execute("LOCK TABLE tunnels_message IN ACCESS EXCLUSIVE MODE")
    print('locked', flush=True)
    connection.execute('SELECT pg_sleep(%s)', (int(sys.argv[1]),))
    connection.rollback()
"""


class DiagnosticLock(AbstractContextManager[None]):
    """Release the diagnostic lock on success, failure, or interruption."""

    def __init__(self, seconds: int, root: Path) -> None:
        self.seconds = seconds
        self.root = root
        self.process: subprocess.Popen[str] | None = None

    def __enter__(self) -> None:
        if not self.seconds:
            return
        if not 1 <= self.seconds <= 60:
            raise ValueError("The diagnostic lock duration must be from 1 through 60 seconds.")
        self.process = subprocess.Popen(  # nosec B603
            [sys.executable, "-c", LOCK_PROGRAM, str(self.seconds)],
            cwd=self.root,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert self.process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if (
                    not selector.select(timeout=10)
                    or self.process.stdout.readline().strip() != "locked"
                ):
                    raise ValueError("The diagnostic lock could not start.")
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *arguments: object) -> None:
        if self.process is None:
            return
        try:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        finally:
            if self.process.stdout is not None:
                self.process.stdout.close()
