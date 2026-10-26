from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import Membership
from apps.scheduling import billing
from apps.scheduling.models import (
    Appointment, Category, Client, Plan, Service, Subscription, WorkSchedule,
)
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
# A weekday afternoon after the billing cutoff, so turns sit on both sides of
# "now" and unpaid turns count as debt.
NOON = datetime(2026, 11, 4, 13, 0, tzinfo=ASUNCION)


def frozen(moment):
    return mock.patch('django.utils.timezone.now', return_value=moment)


def seed(moment=NOON):
    with frozen(moment):
        call_command('seed_showcase')


def showcase():
    return Tenant.objects.get(slug='showcase')


@pytest.fixture
def dev(settings):
    settings.DEBUG = True  # test settings ship DEBUG=False; the seeder is dev-only


@pytest.mark.django_db
def test_refuses_in_prod(settings):
    settings.DEBUG = False
    with pytest.raises(CommandError):
        call_command('seed_showcase')
    assert not Tenant.objects.exists()


@pytest.mark.django_db
def test_stable_rows_are_idempotent(dev):
    models = (
        Tenant, get_user_model(), Membership, WorkSchedule, Category, Service, Client,
        Plan, Subscription,
    )
    seed()
    counts = {model: model.objects.count() for model in models}

    seed(NOON + timedelta(days=1))

    for model, first in counts.items():
        assert model.objects.count() == first, f'{model.__name__} duplicated on re-run'


@pytest.mark.django_db
def test_turns_follow_today(dev):
    seed()
    later = NOON + timedelta(weeks=2)
    seed(later)

    today = later.date()
    days = [a.start.astimezone(ASUNCION).date() for a in Appointment.objects.filter(tenant=showcase())]
    assert days
    assert all(today - timedelta(days=14) <= day <= today + timedelta(days=7) for day in days)
    todays = [
        a for a in Appointment.objects.filter(tenant=showcase())
        if a.start.astimezone(ASUNCION).date() == today
    ]
    assert any(a.start < later for a in todays)
    assert any(a.start > later for a in todays)


@pytest.mark.django_db
def test_other_tenants_are_untouched(dev):
    with frozen(NOON):
        call_command('seed_demo')
    demo = set(Appointment.objects.filter(tenant__slug='demo').values_list('id', flat=True))
    assert demo

    seed()
    seed(NOON + timedelta(days=3))

    assert set(Appointment.objects.filter(tenant__slug='demo').values_list('id', flat=True)) == demo


@pytest.mark.django_db
def test_one_market(dev):
    seed()
    tenant = showcase()
    assert tenant.country == 'PY'
    assert tenant.timezone == 'America/Asuncion'
    phones = [str(c.phone) for c in Client.objects.for_tenant(tenant)]
    assert phones and all(phone.startswith('+595') for phone in phones)


@pytest.mark.django_db
def test_something_to_show(dev):
    seed()
    tenant = showcase()
    turns = Appointment.objects.filter(tenant=tenant)

    assert turns.filter(status=Appointment.Status.PENDING).count() == 1
    unclosed = turns.filter(status=Appointment.Status.SCHEDULED, end__lt=NOON).count()
    assert 2 <= unclosed <= 3
    with frozen(NOON):
        owed = billing.receivables(tenant)
    assert 3 <= len(owed['rows']) <= 5
    assert len(owed['due_soon']) == 1
