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


def test_the_same_stretch_cannot_be_stored_twice(salon, stylist):
    shift(salon, stylist, start=time(9), end=time(12))

    with pytest.raises(IntegrityError), transaction.atomic():
        shift(salon, stylist, start=time(9), end=time(18))


def test_a_shift_cannot_end_before_it_starts(salon, stylist):
    with pytest.raises(IntegrityError), transaction.atomic():
        shift(salon, stylist, start=time(18), end=time(9))


def test_time_off_cannot_end_before_it_starts(salon, stylist):
    start = timezone.now()

    with pytest.raises(IntegrityError), transaction.atomic():
        TimeOff.objects.create(
            tenant=salon, professional=stylist, start=start, end=start - timedelta(hours=1),
        )
