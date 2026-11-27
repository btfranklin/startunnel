from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.migrations.state import StateApps
from django.db.models.functions import Upper


def check_username_collisions(apps: StateApps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    user = apps.get_model("accounts", "User")
    connection = schema_editor.connection
    if connection.vendor == "postgresql":
        table = schema_editor.quote_name(user._meta.db_table)
        schema_editor.execute(f"LOCK TABLE {table} IN SHARE MODE")
    if (
        user.objects.using(connection.alias)
        .order_by()
        .values(normalized=Upper("username"))
        .annotate(total=models.Count("pk"))
        .filter(total__gt=1)
        .exists()
    ):
        raise RuntimeError(
            "Admin usernames differ only in case. "
            "Give each account a distinct username before you run this migration again."
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0004_adminreceipt_admin_beacon_receipt_resource"),
    ]

    operations = [
        migrations.RunPython(check_username_collisions, reverse_code=migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                Upper("username"), name="unique_admin_username_upper"
            ),
        ),
    ]
