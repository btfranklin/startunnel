"""Skip cycle-root validation for child message writes."""

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor

CHECK_CYCLE_ROOT = """
CREATE OR REPLACE FUNCTION public.startunnel_check_cycle_root()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'tunnels_cycle' THEN
        PERFORM public.startunnel_validate_cycle_root_id(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END
        );
    ELSE
        IF (
            CASE WHEN TG_OP = 'DELETE' THEN OLD.parent_id ELSE NEW.parent_id END
        ) IS NOT NULL THEN
            RETURN NULL;
        END IF;
        PERFORM public.startunnel_validate_cycle_root_id(
            CASE WHEN TG_OP = 'DELETE' THEN OLD.cycle_id ELSE NEW.cycle_id END
        );
        IF TG_OP = 'UPDATE' AND OLD.cycle_id IS DISTINCT FROM NEW.cycle_id THEN
            PERFORM public.startunnel_validate_cycle_root_id(OLD.cycle_id);
        END IF;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SET search_path = pg_catalog, public;
"""


def replace_cycle_root_guard(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    del apps
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(CHECK_CYCLE_ROOT)


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0006_remove_message_content_digest")]

    operations = [migrations.RunPython(replace_cycle_root_guard)]
