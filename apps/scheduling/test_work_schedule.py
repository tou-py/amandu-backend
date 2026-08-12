"""
The two tables that say when a professional is open for business.

There is no availability calculation yet -- these lock the shape it will read:
the weekday convention, the split shift, and the guards that keep a nonsensical
stretch out of the table in the first place.
"""

from datetime import date, time, timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.accounts.models import Membership
from apps.scheduling.models import TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

MONDAY = 0
TUESDAY = 1


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY')


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


def shift(salon, stylist, weekday=MONDAY, start=time(9), end=time(12)):
    return WorkSchedule.objects.create(
        tenant=salon,
        professional=stylist,
        weekday=weekday,
        start_time=start,
        end_time=end,
    )


def test_weekday_matches_python_not_django(salon, stylist):
    """
    The convention the whole availability calculation hangs on.

    Python's date.weekday() is Monday=0; Django's __week_day lookup is Sunday=1.
    Both are one import away, they differ silently, and a schedule stored in the
    wrong one is only discovered when a client books a closed day.
    """
    row = shift(salon, stylist, weekday=MONDAY)

    # 2026-08-10 is a Monday.
    assert date(2026, 8, 10).weekday() == row.weekday
    assert row.get_weekday_display() == 'Monday'


def test_a_day_can_hold_two_stretches(salon, stylist):
    """The lunch break: 09:00-12:00 and 14:00-19:00 are one Monday, not two."""
    shift(salon, stylist, start=time(9), end=time(12))
    shift(salon, stylist, start=time(14), end=time(19))

    assert WorkSchedule.objects.filter(professional=stylist, weekday=MONDAY).count() == 2


def test_the_same_stretch_cannot_be_stored_twice(salon, stylist):
    shift(salon, stylist, start=time(9), end=time(12))

    with pytest.raises(IntegrityError), transaction.atomic():
        shift(salon, stylist, start=time(9), end=time(18))


def test_two_professionals_keep_their_own_weeks(salon, stylist, django_user_model):
    """A salon of five is five schedules, which is why this hangs off the
    membership and not off the tenant."""
    other_user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    other = Membership.objects.create(user=other_user, tenant=salon, attends_appointments=True)

    shift(salon, stylist, weekday=MONDAY)
    shift(salon, other, weekday=MONDAY)

    assert WorkSchedule.objects.filter(weekday=MONDAY).count() == 2


def test_a_shift_cannot_end_before_it_starts(salon, stylist):
    with pytest.raises(IntegrityError), transaction.atomic():
        shift(salon, stylist, start=time(18), end=time(9))


def test_time_off_without_a_professional_shuts_the_whole_tenant(salon):
    """Christmas is one row, and it stays true for whoever is hired in March."""
    start = timezone.now()

    closed = TimeOff.objects.create(
        tenant=salon, start=start, end=start + timedelta(days=1), reason='Holiday',
    )

    assert closed.professional is None


def test_time_off_cannot_end_before_it_starts(salon, stylist):
    start = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        TimeOff.objects.create(
            tenant=salon, professional=stylist, start=start, end=start - timedelta(hours=1),
        )
