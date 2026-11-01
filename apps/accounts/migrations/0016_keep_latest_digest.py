from django.db import migrations
from django.db.models import Max


def keep_latest(apps, schema_editor):
    """The digest and the billing notice now replace their previous row
    (apps/scheduling/notify.py REPLACES); clear the ones filed before that."""
    Notification = apps.get_model('accounts', 'Notification')
    for verb in ('plan_digest', 'billing_due'):
        rows = Notification.objects.filter(verb=verb)
        latest = rows.values('recipient').annotate(last=Max('created_at'))
        for one in latest:
            rows.filter(recipient=one['recipient'], created_at__lt=one['last']).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0015_membership_photo_key_unique'),
    ]

    operations = [
        migrations.RunPython(keep_latest, migrations.RunPython.noop),
    ]
