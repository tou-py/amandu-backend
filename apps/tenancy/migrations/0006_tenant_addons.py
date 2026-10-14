from django.db import migrations, models


def keep_cobros_for_tenants_using_it(apps, schema_editor):
    """
    Cobros became a paid add-on after the pilots started using it. Defaulting
    the flag to False is right for a tenant that arrives tomorrow and wrong for
    one that already has its money in it: it would wake up to its plans and its
    debts hidden. So any tenant with a plan, a subscription or a cash entry
    keeps it, and the decision to charge for it is a conversation, not a deploy.
    """
    Tenant = apps.get_model('tenancy', 'Tenant')
    Plan = apps.get_model('scheduling', 'Plan')
    Subscription = apps.get_model('scheduling', 'Subscription')
    CashEntry = apps.get_model('accounting', 'CashEntry')

    using = (
        set(Plan.objects.values_list('tenant_id', flat=True))
        | set(Subscription.objects.values_list('tenant_id', flat=True))
        | set(CashEntry.objects.values_list('tenant_id', flat=True))
    )
    Tenant.objects.filter(pk__in=using).update(addon_cobros=True)


class Migration(migrations.Migration):

    dependencies = [
        ('tenancy', '0005_tenant_plan_settings'),
        ('scheduling', '0017_subscription_anchor_day'),
        ('accounting', '0003_cashentry_recorded_by'),
    ]

    operations = [
        migrations.AddField(
            model_name='tenant',
            name='addon_cobros',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenant',
            name='addon_facturacion',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='tenant',
            name='billing_notice_date',
            field=models.DateField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(keep_cobros_for_tenants_using_it, migrations.RunPython.noop),
    ]
