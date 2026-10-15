"""
The data half of 0006_tenant_addons: a pilot already using Cobros keeps it.
Run against the function itself with the real app registry -- the migration
framework adds nothing to what is being checked, which is the selection.
"""
import importlib
from datetime import date

from django.apps import apps as django_apps

from apps.accounting.models import CashEntry
from apps.scheduling.models import Client, Plan, Subscription
from apps.tenancy.models import Tenant

migration = importlib.import_module('apps.tenancy.migrations.0006_tenant_addons')


def test_tenants_with_money_in_kyo_keep_cobros(db):
    with_plan = Tenant.objects.create(name='Plans', slug='plans')
    Plan.objects.create(tenant=with_plan, name='Pilates', price=250000)

    with_subscription = Tenant.objects.create(name='Subs', slug='subs')
    plan = Plan.objects.create(tenant=with_subscription, name='Yoga', price=200000)
    client = Client.objects.create(tenant=with_subscription, name='Ana')
    Subscription.objects.create(tenant=with_subscription, client=client, plan=plan, start_date=date(2026, 9, 1))

    with_cash = Tenant.objects.create(name='Cash', slug='cash')
    CashEntry.objects.create(
        tenant=with_cash, kind='income', amount=90000, occurred_on=date(2026, 9, 1), concept='Corte'
    )

    untouched = Tenant.objects.create(name='Fresh', slug='fresh')

    migration.keep_cobros_for_tenants_using_it(django_apps, None)

    on = set(Tenant.objects.filter(addon_cobros=True).values_list('slug', flat=True))
    assert on == {'plans', 'subs', 'cash'}
    untouched.refresh_from_db()
    assert untouched.addon_cobros is False
