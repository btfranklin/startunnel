from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tunnels", "0002_auditevent_actor_admin_credential_auditevent_channel")]

    operations = [
        migrations.AddField(
            model_name="message",
            name="session",
            field=models.CharField(blank=True, max_length=32, null=True),
        ),
    ]
