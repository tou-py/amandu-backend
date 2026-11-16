"""
The availability calculation: what the three tables add up to.

Every time here is written in the tenant's zone (America/Asuncion) on purpose --
that is how a shop owner states its hours, and a test that reasons in UTC would
agree with a bug that shifts the whole day.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from apps.accounts.models import Membership
from apps.scheduling.availability import free_slots
from apps.scheduling.models import Appointment, Client, Service, TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
# A Monday, chosen once so every test states the weekday it means.
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)
# Far enough back that "now" never accidentally filters a slot out.
BEFORE = datetime(2026, 8, 1, tzinfo=ASUNCION)


def local(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY', timezone='America/Asuncion',
    )


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


@pytest.fixture
def morning(db, salon, stylist):
    """Mondays, 09:00 to 11:00. Four haircuts fit."""
    return WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(11),
    )


def book(salon, stylist, haircut, start, minutes=30, status=Appointment.Status.SCHEDULED):
    return Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=start, end=start + timedelta(minutes=minutes), status=status,
    )


def slots(stylist, haircut, day, now=BEFORE):
    return free_slots(stylist, haircut, day, day, now=now)[day]


def test_an_open_stretch_is_sliced_by_the_service_duration(stylist, haircut, morning):
    assert slots(stylist, haircut, MONDAY) == [
        local(MONDAY, 9), local(MONDAY, 9, 30), local(MONDAY, 10), local(MONDAY, 10, 30),
    ]


def test_a_day_with_no_schedule_is_closed_not_missing(stylist, haircut, morning):
    """The key exists and is empty. A caller drawing a week has to be able to
    tell "closed" from "I did not ask about that day"."""
    week = free_slots(stylist, haircut, MONDAY, TUESDAY, now=BEFORE)

    assert week[TUESDAY] == []
    assert set(week) == {MONDAY, TUESDAY}


def test_the_lunch_break_is_a_hole_in_the_day(salon, stylist, haircut, morning):
    """Two stretches on one weekday, and nothing on offer in between."""
    WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(14), end_time=time(15),
    )

    assert slots(stylist, haircut, MONDAY) == [
        local(MONDAY, 9), local(MONDAY, 9, 30), local(MONDAY, 10), local(MONDAY, 10, 30),
        local(MONDAY, 14), local(MONDAY, 14, 30),
    ]


def test_a_booking_takes_its_slot_off_the_board(salon, stylist, haircut, morning):
    book(salon, stylist, haircut, local(MONDAY, 9, 30))

    assert slots(stylist, haircut, MONDAY) == [
        local(MONDAY, 9), local(MONDAY, 10), local(MONDAY, 10, 30),
    ]


def test_a_cancelled_booking_gives_the_slot_back(salon, stylist, haircut, morning):
    book(salon, stylist, haircut, local(MONDAY, 9, 30), status=Appointment.Status.CANCELLED)

    assert local(MONDAY, 9, 30) in slots(stylist, haircut, MONDAY)


def test_an_off_grid_booking_packs_the_rest_against_it(salon, stylist, haircut, morning):
    """
    A 09:15 booking pushes the next opening to 09:45, not to 10:00.

    Stepping over by a fixed slot would invent a fifteen-minute hole nobody can
    sell -- the dead gap the whole calculation exists to avoid.
    """
    book(salon, stylist, haircut, local(MONDAY, 9, 15))

    assert slots(stylist, haircut, MONDAY) == [
        local(MONDAY, 9, 45), local(MONDAY, 10, 15),
    ]


def test_every_slot_comes_back_in_the_tenants_zone(salon, stylist, haircut, morning):
    """
    Comparing datetimes compares instants, so a slot in the wrong zone still
    passes every other test in this file. It is the rendered string that betrays
    it -- and a booking page is nothing but rendered strings.

    The booking exists to force the cursor over an obstacle, which is where the
    zone used to be lost: that end came off a database row, in UTC.
    """
    book(salon, stylist, haircut, local(MONDAY, 9, 15))

    assert {slot.utcoffset() for slot in slots(stylist, haircut, MONDAY)} == {
        timedelta(hours=-3),
    }


def test_back_to_back_bookings_are_stepped_over_in_one_go(salon, stylist, haircut, morning):
    book(salon, stylist, haircut, local(MONDAY, 9), minutes=45)
    book(salon, stylist, haircut, local(MONDAY, 9, 45), minutes=45)

    assert slots(stylist, haircut, MONDAY) == [local(MONDAY, 10, 30)]


def test_time_off_for_the_person_clears_their_morning(salon, stylist, haircut, morning):
    TimeOff.objects.create(
        tenant=salon, professional=stylist,
        start=local(MONDAY, 9), end=local(MONDAY, 10), reason='Doctor',
    )

    assert slots(stylist, haircut, MONDAY) == [local(MONDAY, 10), local(MONDAY, 10, 30)]


def test_time_off_with_no_professional_shuts_everyone(salon, stylist, haircut, morning):
    """The holiday row names nobody, and it still has to close this person."""
    TimeOff.objects.create(
        tenant=salon, professional=None,
        start=local(MONDAY, 0), end=local(TUESDAY, 0), reason='Holiday',
    )

    assert slots(stylist, haircut, MONDAY) == []


def test_a_slot_already_past_is_not_on_offer(stylist, haircut, morning):
    """A public page offering this morning at ten, at noon, is a booking that
    cannot be honoured."""
    assert slots(stylist, haircut, MONDAY, now=local(MONDAY, 10)) == [
        local(MONDAY, 10), local(MONDAY, 10, 30),
    ]


def test_a_service_longer_than_the_stretch_never_fits(salon, stylist, morning):
    all_day = Service.objects.create(
        tenant=salon, name='Colour', duration=timedelta(hours=3),
    )

    assert slots(stylist, all_day, MONDAY) == []


def test_another_professionals_day_is_not_this_ones(salon, stylist, haircut, morning,
                                                    django_user_model):
    """Schedules hang off the membership, so a colleague being booked solid
    takes nothing off this person's board."""
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    other = Membership.objects.create(user=user, tenant=salon, attends_appointments=True)
    book(salon, other, haircut, local(MONDAY, 9))

    assert local(MONDAY, 9) in slots(stylist, haircut, MONDAY)


def test_the_query_count_does_not_grow_with_the_window(salon, stylist, haircut, morning,
                                                       django_assert_num_queries):
    """Three queries for a day, three for a month. The loop is in Python on
    purpose; asking per day is where this turns into an N+1."""
    with django_assert_num_queries(3):
        free_slots(stylist, haircut, MONDAY, MONDAY + timedelta(days=30), now=BEFORE)
