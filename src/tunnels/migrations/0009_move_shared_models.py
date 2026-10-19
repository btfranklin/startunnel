"""Move shared record state while keeping its physical tables."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0008_beacon_board")]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.DeleteModel(name="AuditEvent"),
                migrations.DeleteModel(name="IdempotencyRecord"),
            ],
        ),
    ]
