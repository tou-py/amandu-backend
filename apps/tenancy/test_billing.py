"""
Manual billing: the month arithmetic and what confirming a transfer does.

The suspension side lives with the sweep that performs it
(apps/scheduling/test_send_reminders.py) -- this covers the other half, the one
a human triggers from the admin.
"""
from datetime import date, timedelta

import pytest
from django.utils import timezone

from apps.tenancy.admin import TenantAdmin, one_month_after
from apps.tenancy.models import Tenant


@pytest.mark.parametrize(
    'start, expected',
    [
        (date(2026, 1, 15), date(2026, 2, 15)),
        # December rolls the year, which is where naive month + 1 breaks.
        (date(2026, 12, 5), date(2027, 1, 5)),
        # The clamp: February has no 31st, and the answer is its last day, not
        # a crash and not a date in March.
        (date(2026, 1, 31), date(2026, 2, 28)),
        # ...and it knows which Februaries have 29.
        (date(2028, 1, 31), date(2028, 2, 29)),
        (date(2026, 3, 31), date(2026, 4, 30)),
    ],
)
def test_one_month_after(start, expected):
    assert one_month_after(start) == expected


@pytest.fixture
def admin_action(rf, admin_user):
    """Runs the admin action the way the admin does, against a real queryset."""
    from django.contrib import admin as django_admin

    def run(name, queryset):
        request = rf.post('/admin/tenancy/tenant/')
        request.user = admin_user
        # message_user writes to the message framework, which needs somewhere to
        # write; the admin request has a session and this one does not.
        request._messages = type('Sink', (), {'add': lambda *a, **kw: None})()
        model_admin = TenantAdmin(Tenant, django_admin.site)
        getattr(model_admin, name)(request, queryset)

    return run


@pytest.fixture
def tenant(db):
    def make(paid_until=None, status=Tenant.Status.ACTIVE):
        return Tenant.objects.create(
            name='Studio', slug='studio', status=status, paid_until=paid_until
        )
    return make


def test_registering_a_payment_extends_a_month_from_today_when_lapsed(tenant, admin_action):
    """A tenant who paid late does not owe us the days they spent locked out:
    the new period starts now, not where the old one died."""
    today = timezone.localdate()
    studio = tenant(paid_until=today - timedelta(days=20))

    admin_action('register_monthly_payment', Tenant.objects.filter(pk=studio.pk))

    studio.refresh_from_db()
    assert studio.paid_until == one_month_after(today)


def test_registering_a_payment_stacks_on_time_still_left(tenant, admin_action):
    """Paying early must not throw away what is unused, or being punctual costs
    the customer days."""
    today = timezone.localdate()
    remaining = today + timedelta(days=10)
    studio = tenant(paid_until=remaining)

    admin_action('register_monthly_payment', Tenant.objects.filter(pk=studio.pk))

    studio.refresh_from_db()
    assert studio.paid_until == one_month_after(remaining)


def test_registering_a_payment_lifts_a_suspension(tenant, admin_action):
    studio = tenant(paid_until=timezone.localdate() - timedelta(days=3), status=Tenant.Status.SUSPENDED)

    admin_action('register_monthly_payment', Tenant.objects.filter(pk=studio.pk))

    studio.refresh_from_db()
    assert studio.status == Tenant.Status.ACTIVE


def test_registering_a_payment_does_not_reopen_a_closed_tenant(tenant, admin_action):
    """CLOSED means they left. Money arriving does not undo that on its own --
    somebody has to decide to take them back."""
    studio = tenant(status=Tenant.Status.CLOSED)

    admin_action('register_monthly_payment', Tenant.objects.filter(pk=studio.pk))

    studio.refresh_from_db()
    assert studio.status == Tenant.Status.CLOSED
    # The period still moved: what is being tested is that access did not.
    assert studio.paid_until == one_month_after(timezone.localdate())
