"""Write administrator secrets to new private files."""

import os
from pathlib import Path

from django.core.management.base import CommandError


def reserve_key_file(value: str) -> tuple[Path, int]:
    path = Path(value).expanduser().absolute()
    if not path.parent.is_dir() or any(parent.is_symlink() for parent in path.parents):
        raise CommandError("The output directory is not available.")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError as error:
        raise CommandError("Use a new regular key file in a private directory.") from error
    return path, descriptor
