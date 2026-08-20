from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0012_remove_late_cancel_attendance'),
    ]

    operations = [
        migrations.AddField(
            model_name='appointment',
            name='rescheduled_from',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
