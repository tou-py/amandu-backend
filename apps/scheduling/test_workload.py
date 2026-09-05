"""
The day-load calculation, and the endpoint over it.

Times are written in the tenant's zone on purpose -- that is how a shop states
its day, and a test reasoning in UTC would agree with a bug that shifts it.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service
from apps.scheduling.workload import daily_load
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
MONDAY = date(2026, 8, 10)
TUESDAY = date(2026, 8, 11)


def local(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


def span(day, from_hour, to_hour):
    return (local(day, from_hour), local(day, to_hour))


# --- the arithmetic -------------------------------------------------------


def test_a_single_booking_is_its_own_length():
    [row] = daily_load([span(MONDAY, 9, 10)], ASUNCION)
    assert row == {'date': MONDAY, 'count': 1, 'busy_minutes': 60}


def test_two_professionals_in_the_same_hour_are_one_busy_hour():
    # The whole reason this is a union. Summing would let a three-chair salon
    # report a 300% day.
    [row] = daily_load([span(MONDAY, 9, 10), span(MONDAY, 9, 10)], ASUNCION)
    assert row['busy_minutes'] == 60
    assert row['count'] == 2, 'both are still two appointments'


def test_a_short_booking_inside_a_long_one_does_not_extend_it():
    # Sorted by start, the 10-11 span is seen after 9-13 and ends earlier.
    # Letting the union mark slide back to 11 would re-count 11-13.
    [row] = daily_load([span(MONDAY, 9, 13), span(MONDAY, 10, 11)], ASUNCION)
    assert row['busy_minutes'] == 4 * 60


def test_back_to_back_bookings_make_one_stretch():
    [row] = daily_load([span(MONDAY, 9, 10), span(MONDAY, 10, 11)], ASUNCION)
    assert row['busy_minutes'] == 2 * 60


def test_a_gap_between_bookings_is_not_worked():
    [row] = daily_load([span(MONDAY, 9, 10), span(MONDAY, 15, 16)], ASUNCION)
    assert row['busy_minutes'] == 2 * 60


def test_each_day_is_counted_on_its_own():
    monday, tuesday = daily_load([span(MONDAY, 9, 10), span(TUESDAY, 9, 12)], ASUNCION)
    assert (monday['date'], monday['busy_minutes']) == (MONDAY, 60)
    assert (tuesday['date'], tuesday['busy_minutes']) == (TUESDAY, 180)


def test_days_come_back_in_order():
    rows = daily_load([span(TUESDAY, 9, 10), span(MONDAY, 9, 10)], ASUNCION)
    assert [row['date'] for row in rows] == [MONDAY, TUESDAY]


def test_a_booking_across_midnight_splits_its_minutes_but_counts_once():
    # 23:00 Monday to 01:00 Tuesday. One appointment, an hour on each day.
    rows = daily_load([(local(MONDAY, 23), local(TUESDAY, 1))], ASUNCION)
    assert [(row['date'], row['count'], row['busy_minutes']) for row in rows] == [
        (MONDAY, 1, 60),
        (TUESDAY, 0, 60),
    ]


def test_days_are_local_days_not_utc_ones():
    # 22:00 in Asuncion (UTC-4) is 02:00 the NEXT day in UTC. Read as a UTC day
    # this lands on Tuesday, and the shop would see its late booking on the
    # wrong side of midnight.
    [row] = daily_load([span(MONDAY, 22, 23)], ASUNCION)
    assert row['date'] == MONDAY


def test_a_zero_length_row_is_ignored():
    assert daily_load([span(MONDAY, 9, 9)], ASUNCION) == []


def test_nothing_booked_is_no_rows_at_all():
    assert daily_load([], ASUNCION) == []


# --- the endpoint ---------------------------------------------------------


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY', timezone='America/Asuncion',
    )


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(
        user=user, tenant=salon, attends_appointments=True, role=Membership.Role.OWNER,
    )


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=60))


@pytest.fixture
def someone(db, salon):
    return Client.objects.create(tenant=salon, name='Ada', phone='+595981111111')


@pytest.fixture
def api(stylist, salon):
    client = APIClient()
    client.force_authenticate(user=stylist.user)
    # The header the front end actually sends, and the pk it sends -- not the
    # slug. Without it these would pass on the single-membership fallback, and
    # the cross-tenant test below would be proving nothing.
    client.credentials(HTTP_X_TENANT_ID=str(salon.pk))
    return client


def book(salon, stylist, haircut, day, hour, status=Appointment.Status.SCHEDULED):
    start = local(day, hour)
    return Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=start, end=start + haircut.duration, status=status,
    )


def summary(api, day_from=MONDAY, day_to=TUESDAY):
    return api.get(
        '/api/appointments/summary/',
        {'from': day_from.isoformat(), 'to': day_to.isoformat()},
    )


@pytest.mark.django_db
def test_summary_returns_one_row_per_busy_day(api, salon, stylist, haircut):
    book(salon, stylist, haircut, MONDAY, 9)
    book(salon, stylist, haircut, MONDAY, 10)
    book(salon, stylist, haircut, TUESDAY, 9)

    response = summary(api)

    assert response.status_code == 200
    assert response.json() == [
        {'date': MONDAY.isoformat(), 'count': 2, 'busy_minutes': 120},
        {'date': TUESDAY.isoformat(), 'count': 1, 'busy_minutes': 60},
    ]


@pytest.mark.django_db
def test_a_cancelled_slot_held_nothing(api, salon, stylist, haircut):
    book(salon, stylist, haircut, MONDAY, 9)
    book(salon, stylist, haircut, MONDAY, 11, status=Appointment.Status.CANCELLED)

    [row] = summary(api).json()

    assert row == {'date': MONDAY.isoformat(), 'count': 1, 'busy_minutes': 60}


@pytest.mark.django_db
def test_a_pending_request_still_holds_its_hour(api, salon, stylist, haircut):
    # Nobody has agreed to it, but the overlap constraint keeps the slot for it,
    # so the day really is that much less free.
    book(salon, stylist, haircut, MONDAY, 9, status=Appointment.Status.PENDING)

    [row] = summary(api).json()

    assert row['count'] == 1
    assert row['busy_minutes'] == 60


@pytest.mark.django_db
def test_an_empty_day_is_absent_rather_than_zero(api, salon, stylist, haircut):
    book(salon, stylist, haircut, TUESDAY, 9)

    assert [row['date'] for row in summary(api).json()] == [TUESDAY.isoformat()]


@pytest.mark.django_db
def test_another_tenant_is_never_counted(api, salon, stylist, haircut, django_user_model):
    other = Tenant.objects.create(
        name='Other', slug='other', country='PY', timezone='America/Asuncion',
    )
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    their_stylist = Membership.objects.create(
        user=user, tenant=other, attends_appointments=True,
    )
    their_service = Service.objects.create(
        tenant=other, name='Cut', duration=timedelta(minutes=60),
    )
    book(other, their_stylist, their_service, MONDAY, 9)

    assert summary(api).json() == []


@pytest.mark.django_db
def test_the_range_is_required(api, salon):
    assert api.get('/api/appointments/summary/').status_code == 400


@pytest.mark.django_db
def test_a_backwards_range_is_refused(api, salon):
    assert summary(api, day_from=TUESDAY, day_to=MONDAY).status_code == 400


@pytest.mark.django_db
def test_an_unbounded_scrape_is_refused(api, salon):
    response = summary(api, day_to=MONDAY + timedelta(days=400))
    assert response.status_code == 400


@pytest.mark.django_db
def test_summary_costs_one_query(api, salon, stylist, haircut, django_assert_num_queries):
    """
    The whole point of the endpoint. The list route pages through full slots with
    their service, professional and roster attached; this reads two columns once.
    """
    for hour in range(9, 15):
        book(salon, stylist, haircut, MONDAY, hour)

    # Two: resolving the membership, which every authenticated route pays, and
    # the aggregate itself. Six bookings over six hours and still two, which is
    # the property worth pinning -- a regression that reads per day, or per row,
    # shows up here as a jump rather than as a slow screen nobody traces back.
    with django_assert_num_queries(2):
        summary(api)
