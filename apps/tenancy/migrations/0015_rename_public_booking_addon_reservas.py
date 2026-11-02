from django.db import migrations


class Migration(migrations.Migration):
    """A rename, so every business whose page is open today keeps it."""

    dependencies = [
        ('tenancy', '0014_tenant_trial_until'),
    ]

    operations = [
        migrations.RenameField(
            model_name='tenant',
            old_name='public_booking',
            new_name='addon_reservas',
        ),
    ]
