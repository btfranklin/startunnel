"""Print non-sensitive effective runtime settings."""

from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from core.limits import Limits
from core.maintenance_status import read_maintenance_status


class Command(BaseCommand):
    help = "Print a redacted runtime configuration summary."

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
        limits = Limits()
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
                "api_operations_per_minute": limits.api_operations_per_minute,
                "messages_per_cycle": limits.messages_per_cycle,
            },
            "static": {"root": str(settings.STATIC_ROOT), "url": settings.STATIC_URL},
            "secrets": "redacted",
        }
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True, default=str))
