from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("agents", "0001_initial"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="agentcredential",
            name="suspended_at",
        ),
    ]
