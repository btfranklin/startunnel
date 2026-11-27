"""Print non-sensitive effective runtime settings."""

from __future__ import annotations

import json
from argparse import ArgumentParser

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core import limits as instance_limits
from core.maintenance_status import read_maintenance_status


class Command(BaseCommand):
    help = "Print a redacted runtime configuration summary."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--require-ready", action="store_true")

    def handle(self, *args: object, **options: object) -> None:
        database = settings.DATABASES["default"]
        postgres_state = "unavailable"
        migration_state = "unknown"
        database_role = "unknown"
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT current_user")
                row = cursor.fetchone()
            postgres_state = "ready"
            database_role = str(row[0]) if row else "unknown"
            executor = MigrationExecutor(connection)
            migration_state = (
                "ready"
                if not executor.migration_plan(executor.loader.graph.leaf_nodes())
                else "pending"
            )
        except Exception:
            migration_state = "unavailable"
        maintenance_state = "unavailable"
        try:
            maintenance_status = read_maintenance_status()
            if maintenance_status.current:
                maintenance_state = "ready"
        except Exception:
            maintenance_state = "unavailable"
        limits = instance_limits.effective_limits()
        output = {
            "environment": settings.SETTINGS_MODULE,
            "debug": settings.DEBUG,
            "allowed_hosts": settings.ALLOWED_HOSTS,
            "database": {
                "engine": database.get("ENGINE"),
                "host": database.get("HOST") or "local",
                "port": database.get("PORT") or "default",
                "name": database.get("NAME"),
                "vendor": connection.vendor,
                "role": database_role,
                "status": postgres_state,
            },
            "migrations": migration_state,
            "maintenance": maintenance_state,
            "identity": {"provider": "local"},
            "limits": {
                "active_credentials": limits.active_credentials,
                "api_operations_per_minute": limits.api_operations_per_minute,
                "messages_per_cycle": limits.messages_per_cycle,
            },
            "static": {"root": str(settings.STATIC_ROOT), "url": settings.STATIC_URL},
            "secrets": "redacted",
        }
        if options.get("require_ready"):
            if (
                connection.vendor != "postgresql"
                or postgres_state != "ready"
                or database_role != "startunnel"
                or migration_state != "ready"
                or maintenance_state != "ready"
            ):
                raise CommandError(
                    "Runtime is not ready "
                    f"(database={postgres_state}, runtime_role={database_role == 'startunnel'}, "
                    f"migrations={migration_state}, maintenance={maintenance_state})."
                )
            self.stdout.write("Runtime is ready.")
            return
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True, default=str))
