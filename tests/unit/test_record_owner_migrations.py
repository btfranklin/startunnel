"""Record ownership moves keep content types and permission grants intact."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest
from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.db import connection

from accounts.models import User

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("old_app", "old_model", "new_app", "new_model", "command_rename_first"),
    [
        ("tunnels", "auditevent", "core", "auditevent", False),
        ("tunnels", "idempotencyrecord", "core", "agentreceipt", False),
        ("accounts", "adminoperation", "accounts", "adminreceipt", False),
        ("accounts", "adminoperation", "accounts", "adminreceipt", True),
    ],
)
def test_record_owner_moves_keep_permission_identity_and_joins(
    user_factory: Any,
    old_app: str,
    old_model: str,
    new_app: str,
    new_model: str,
    command_rename_first: bool,
) -> None:
    ContentType.objects.filter(app_label=new_app, model=new_model).delete()
    content_type = ContentType.objects.create(app_label=old_app, model=old_model)
    actions = ("add", "change", "delete", "view")
    permissions = [
        Permission.objects.create(
            content_type=content_type,
            codename=f"{action}_{old_model}",
            name=f"Can {action} old record",
        )
        for action in actions
    ]
    permission_ids = {permission.pk for permission in permissions}
    user = user_factory()
    group = Group.objects.create(name="Record inspection")
    group.permissions.set(permissions)
    user.groups.add(group)
    user.user_permissions.set(permissions)
    user_joins = set(
        user.user_permissions.through.objects.values_list("id", "user_id", "permission_id")
    )
    group_joins = set(
        group.permissions.through.objects.values_list("id", "group_id", "permission_id")
    )
    if old_app == "accounts":
        migration = import_module("accounts.migrations.0003_admin_receipt")
        forwards, backwards = migration.rename_content_type, migration.restore_content_type
    else:
        migration = import_module("core.migrations.0002_shared_records")
        forwards, backwards = migration.move_content_types, migration.restore_content_types

    forwards(apps, connection.schema_editor())

    content_type.refresh_from_db()
    assert (content_type.app_label, content_type.model) == (new_app, new_model)
    assert (
        set(Permission.objects.filter(content_type=content_type).values_list("id", flat=True))
        == permission_ids
    )
    assert set(
        Permission.objects.filter(pk__in=permission_ids).values_list("codename", flat=True)
    ) == {f"{action}_{new_model}" for action in actions}
    assert User.objects.get(pk=user.pk).has_perm(f"{new_app}.view_{new_model}")
    assert (
        set(user.user_permissions.through.objects.values_list("id", "user_id", "permission_id"))
        == user_joins
    )
    assert (
        set(group.permissions.through.objects.values_list("id", "group_id", "permission_id"))
        == group_joins
    )

    if command_rename_first:
        ContentType.objects.filter(pk=content_type.pk).update(model=old_model)
    backwards(apps, connection.schema_editor())

    content_type.refresh_from_db()
    assert (content_type.app_label, content_type.model) == (old_app, old_model)
    assert (
        set(Permission.objects.filter(content_type=content_type).values_list("id", flat=True))
        == permission_ids
    )
    assert set(
        Permission.objects.filter(pk__in=permission_ids).values_list("codename", flat=True)
    ) == {f"{action}_{old_model}" for action in actions}
    assert User.objects.get(pk=user.pk).has_perm(f"{old_app}.view_{old_model}")
    assert (
        set(user.user_permissions.through.objects.values_list("id", "user_id", "permission_id"))
        == user_joins
    )
    assert (
        set(group.permissions.through.objects.values_list("id", "group_id", "permission_id"))
        == group_joins
    )
