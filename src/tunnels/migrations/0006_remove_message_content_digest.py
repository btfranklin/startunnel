"""Remove the unused per-message content digest."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0005_remove_idempotency_derivation_nonce")]

    operations = [
        migrations.RemoveField(
            model_name="message",
            name="content_digest",
        ),
    ]
