"""Name admin retry receipts without changing their table or identity."""

import django.db.models.deletion
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


def _rename_content_type(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
    old_model: str,
    new_model: str,
    verbose_name: str,
) -> None:
    content_types = apps.get_model("contenttypes", "ContentType")
    permissions = apps.get_model("auth", "Permission")
    database = schema_editor.connection.alias
    # Django can reverse its content-type rename before this data operation.
    old = (
        content_types.objects.using(database)
        .filter(app_label="accounts", model__in=[old_model, new_model])
        .first()
    )
    if old is None:
        return
    content_types.objects.using(database).filter(pk=old.pk).update(model=new_model)
    for action in ("add", "change", "delete", "view"):
        permissions.objects.using(database).filter(
            content_type_id=old.pk, codename=f"{action}_{old_model}"
        ).update(codename=f"{action}_{new_model}", name=f"Can {action} {verbose_name}")


def rename_content_type(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_content_type(apps, schema_editor, "adminoperation", "adminreceipt", "admin receipt")


def restore_content_type(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    _rename_content_type(apps, schema_editor, "adminreceipt", "adminoperation", "admin operation")


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_admincredential_adminoperation"),
        ("tunnels", "0008_beacon_board"),
    ]

    operations = [
        migrations.AlterModelTable(name="adminoperation", table="accounts_adminoperation"),
        migrations.RunPython(rename_content_type, restore_content_type),
        migrations.RenameModel(old_name="AdminOperation", new_name="AdminReceipt"),
        migrations.AlterField(
            model_name="adminreceipt",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="admin_receipts",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
