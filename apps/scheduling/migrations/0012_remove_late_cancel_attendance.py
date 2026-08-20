from django.db import migrations, models


def fold_late_cancels_into_no_shows(apps, schema_editor):
    """
    The shops asked for `late_cancel` back out: they never charged it differently
    from a silent absence, so the extra verdict only made the roster harder to
    read. Both mean the same thing to the diary -- the slot was paid for by
    nobody -- so the rows keep the surviving one.
    """
    AppointmentClient = apps.get_model('scheduling', 'AppointmentClient')
    AppointmentClient.objects.filter(attendance='late_cancel').update(attendance='no_show')


def leave_the_no_shows_alone(apps, schema_editor):
    """
    Deliberately does nothing. Once folded, a `no_show` row no longer says
    whether the person warned us, and every rule that could split them back
    apart would mark genuine silent absences as late cancellations. Rolling the
    schema back is safe; the distinction is gone for good.
    """


class Migration(migrations.Migration):

    dependencies = [
        ('scheduling', '0011_appointment_created_by_appointment_source_and_more'),
    ]

    operations = [
        # Before the AlterField on purpose: the value has to be remapped while it
        # is still a legal choice, or the loader validates a column full of rows
        # the field no longer admits.
        migrations.RunPython(fold_late_cancels_into_no_shows, leave_the_no_shows_alone),
        migrations.AlterField(
            model_name='appointmentclient',
            name='attendance',
            field=models.CharField(choices=[('pending', 'Pending'), ('attended', 'Attended'), ('no_show', 'No show')], default='pending', max_length=20),
        ),
    ]
