"""Replace address digests with the original glyph addresses."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.db import migrations, models


def _token(address: str) -> bytes:
    """Validate the fixed glyph format used by the initial schema."""

    values = [ord(character) - 0x1F700 for character in address]
    if len(values) != 24 or any(value < 0 or value > 63 for value in values):
        raise ValueError
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    encoded = "".join(alphabet[value] for value in values[:22])
    token = base64.b64decode(encoded + "==", altchars=b"-_", validate=True)
    if base64.urlsafe_b64encode(token).decode().rstrip("=") != encoded:
        raise ValueError
    checksum = int.from_bytes(hashlib.blake2s(token, digest_size=2).digest(), "big") >> 4
    if values[22:] != [checksum >> 6, checksum & 63]:
        raise ValueError
    return token


def restore_addresses(apps: Any, schema_editor: Any) -> None:
    alias = schema_editor.connection.alias
    records = apps.get_model("tunnels", "IdempotencyRecord").objects.using(alias)
    if records.filter(
        operation__in=["post_reply", "close_cycle", "start_cycle", "rollover_cycle"],
        expires_at__gt=datetime.now(UTC),
    ).exists():
        raise RuntimeError(
            "Stop writers and let address-based operation receipts expire before this migration. "
            "See docs/setup/address-storage.md. No addresses were changed."
        )
    addresses = apps.get_model("tunnels", "TunnelAddress").objects.using(alias)
    if not addresses.exists():
        return
    try:
        path = Path(os.environ["STARTUNNEL_ADDRESS_MIGRATION_FILE"])
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o600:
            raise ValueError
        bundle = json.loads(path.read_text(encoding="utf-8"))
        secret = bundle["address_secret"].encode()
        supplied = bundle["addresses"]
        if not secret or not isinstance(supplied, dict):
            raise ValueError
        # Validate every row before writing. A digest cannot recover a missing address.
        for row in addresses.iterator():
            address = supplied[str(row.pk)]
            token = _token(address)
            if not hmac.compare_digest(
                hmac.digest(secret, token, "sha256"), bytes(row.address_digest)
            ):
                raise ValueError
    except KeyError, OSError, ValueError, TypeError, AttributeError:
        raise RuntimeError(
            "Supply a mode-0600 STARTUNNEL_ADDRESS_MIGRATION_FILE with the old address secret "
            "and every original glyph address. See docs/setup/address-storage.md. "
            "No addresses were changed."
        ) from None
    for row in addresses.iterator():
        addresses.filter(pk=row.pk).update(address=supplied[str(row.pk)])


def replace_address_guard(apps: Any, schema_editor: Any) -> None:
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
        migrations.AddField(
            model_name="tunneladdress",
            name="address",
            field=models.CharField(max_length=24, null=True),
        ),
        migrations.RunPython(restore_addresses),
        migrations.RunPython(replace_address_guard),
        migrations.RemoveConstraint(model_name="tunneladdress", name="unique_tunnel_address"),
        migrations.RemoveField(model_name="tunneladdress", name="address_digest"),
        migrations.AlterField(
            model_name="tunneladdress", name="address", field=models.CharField(max_length=24)
        ),
        migrations.AddConstraint(
            model_name="tunneladdress",
            constraint=models.UniqueConstraint(fields=["address"], name="unique_tunnel_address"),
        ),
    ]
