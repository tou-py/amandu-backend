from zoneinfo import ZoneInfo

from django.db import migrations, models


def date_past_voids(apps, schema_editor):
    """A void happened on the business's calendar, whatever the server's says."""
    CashEntry = apps.get_model('accounting', 'CashEntry')
    voided = CashEntry.objects.filter(voided_at__isnull=False, voided_on__isnull=True).select_related('tenant')
    for entry in voided.iterator():
        day = entry.voided_at.astimezone(ZoneInfo(entry.tenant.timezone)).date()
        CashEntry.objects.filter(pk=entry.pk).update(voided_on=day)


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0004_cashentry_qr_method'),
    ]

    operations = [
        migrations.AddField(
            model_name='cashentry',
            name='voided_on',
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(date_past_voids, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='cashentry',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(voided_at__isnull=True, voided_on__isnull=True)
                    | models.Q(voided_at__isnull=False, voided_on__isnull=False)
                ),
                name='cash_entry_void_has_its_day',
            ),
        ),
    ]
