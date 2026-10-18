"""Remove the nonce after address retries use stored address rows."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0004_store_glyph_addresses")]

    operations = [
        migrations.RemoveField(
            model_name="idempotencyrecord",
            name="derivation_nonce",
        ),
    ]
