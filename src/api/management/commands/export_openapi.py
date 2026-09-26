"""Write the committed OpenAPI artifact."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from api.router import api


class Command(BaseCommand):
    help = "Export the current StarTunnel OpenAPI schema."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--output", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        destination = Path(str(options["output"]))
        destination.parent.mkdir(parents=True, exist_ok=True)
        schema = api.get_openapi_schema()
        destination.write_text(
            json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(f"Wrote {destination}")
