"""Remove old tunnel data before storing plain glyph addresses."""

from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def reset_tunnels(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    alias = schema_editor.connection.alias
    # The historical model was renamed after this migration.
    historical_operations: type[models.Model] = apps.get_model(  # type: ignore[misc]
        "accounts", "AdminOperation"
    )
    historical_operations._default_manager.using(alias).filter(
        operation__startswith="tunnels."
    ).delete()
    tables = (
        "tunnels_messagemention",
        "tunnels_tunnelevent",
        "tunnels_activitycheckpoint",
        "tunnels_idempotencyrecord",
        "tunnels_message",
        "tunnels_cycle",
        "tunnels_tunneladdress",
        "tunnels_tunnel",
    )
    with schema_editor.connection.cursor() as cursor:
        if schema_editor.connection.vendor == "postgresql":
            cursor.execute("TRUNCATE TABLE " + ", ".join(tables))
        else:
            # Table names come from the fixed list above.
            for table in tables:
                cursor.execute(f"DELETE FROM {table}")  # nosec B608


def replace_address_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del apps
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("""
        CREATE OR REPLACE FUNCTION public.startunnel_validate_tunnel_address()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.tunnels_tunnel WHERE id = NEW.tunnel_id
            ) THEN
                RAISE EXCEPTION 'tunnel address must belong to a tunnel';
            END IF;
            IF TG_OP = 'UPDATE'
               AND (
                   NEW.tunnel_id IS DISTINCT FROM OLD.tunnel_id
                   OR NEW.address IS DISTINCT FROM OLD.address
                   OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
               ) THEN
                RAISE EXCEPTION 'tunnel address identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
    """)


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0003_message_session")]

    operations = [
        migrations.RunPython(reset_tunnels),
        migrations.AddField(
            model_name="tunneladdress",
            name="address",
            field=models.CharField(max_length=24),
        ),
        migrations.RunPython(replace_address_guard),
        migrations.RemoveConstraint(model_name="tunneladdress", name="unique_tunnel_address"),
        migrations.RemoveField(model_name="tunneladdress", name="address_digest"),
        migrations.AddConstraint(
            model_name="tunneladdress",
            constraint=models.UniqueConstraint(fields=["address"], name="unique_tunnel_address"),
        ),
    ]
