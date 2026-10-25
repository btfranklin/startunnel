"""Protect stored history and retry receipts from runtime SQL changes."""

from pathlib import Path

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def protect_runtime_history(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del apps
    if schema_editor.connection.vendor == "postgresql":
        source = Path(__file__).with_name("sql") / "runtime-history-guards.sql"
        schema_editor.execute(source.read_text(encoding="utf-8"), params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_adminreceipt_admin_beacon_receipt_resource"),
        ("core", "0003_alter_agentreceipt_resource_id"),
        ("tunnels", "0010_message_message_beacon_responders"),
    ]

    operations = [migrations.RunPython(protect_runtime_history)]
