"""
The endpoints a shop uses to declare when it is open.

Without these the availability calculation reads an empty table and the public
page offers nothing, so this is the surface that makes the whole booking
feature usable by anyone who is not a Django admin.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
SCHEDULE_URL = reverse('scheduling:workschedule-list')
TIME_OFF_URL = reverse('scheduling:timeoff-list')


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


def schedule_detail(row):
    return reverse('scheduling:workschedule-detail', args=[row.pk])


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY', timezone='America/Asuncion',
    )


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(
        name='Clinic', slug='clinic', country='PY', timezone='America/Asuncion',
    )


@pytest.fixture
def owner(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    Membership.objects.create(
        user=user, tenant=salon, role=Membership.Role.OWNER, attends_appointments=True,
    )
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def staff(db, stylist):
    """The stylist's own login: works the floor, does not run the business."""
    return stylist.user


# --- the working week -------------------------------------------------------

def test_the_owner_declares_a_shift(owner, salon, stylist):
    response = api(owner, salon).post(SCHEDULE_URL, {
        'professional': stylist.pk, 'weekday': 0,
        'start_time': '09:00', 'end_time': '12:00',
    }, format='json')

    assert response.status_code == 201
    assert WorkSchedule.objects.get().tenant == salon


def test_a_split_shift_is_two_rows_not_a_conflict(owner, salon, stylist):
    """Nine to twelve and two to seven is one Monday with a lunch break."""
    http = api(owner, salon)
    first = http.post(SCHEDULE_URL, {
        'professional': stylist.pk, 'weekday': 0,
        'start_time': '09:00', 'end_time': '12:00',
    }, format='json')
    second = http.post(SCHEDULE_URL, {
        'professional': stylist.pk, 'weekday': 0,
        'start_time': '14:00', 'end_time': '19:00',
    }, format='json')

    assert (first.status_code, second.status_code) == (201, 201)


def test_a_shift_that_ends_before_it_starts_is_a_400_not_a_500(owner, salon, stylist):
    """The database constraint would raise IntegrityError, which reaches the
    operator as a 500 naming nothing."""
    response = api(owner, salon).post(SCHEDULE_URL, {
        'professional': stylist.pk, 'weekday': 0,
        'start_time': '18:00', 'end_time': '09:00',
    }, format='json')

    assert response.status_code == 400
    assert 'end_time' in response.json()


def test_staff_may_read_the_week_but_not_rewrite_it(staff, salon, stylist):
    """Widening the hours decides what strangers are offered, so it takes
    someone who answers for the business."""
    WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(12),
    )
    http = api(staff, salon)

    assert http.get(SCHEDULE_URL).status_code == 200
    assert http.post(SCHEDULE_URL, {
        'professional': stylist.pk, 'weekday': 1,
        'start_time': '09:00', 'end_time': '12:00',
    }, format='json').status_code == 403


def test_hours_cannot_be_written_into_another_tenants_diary(owner, salon, clinic,
                                                            django_user_model):
    """A guessed membership id from another shop must resolve to nothing."""
    user = django_user_model.objects.create_user(email='x@example.com', password='pw')
    theirs = Membership.objects.create(
        user=user, tenant=clinic, attends_appointments=True,
    )

    response = api(owner, salon).post(SCHEDULE_URL, {
        'professional': theirs.pk, 'weekday': 0,
        'start_time': '09:00', 'end_time': '12:00',
    }, format='json')

    assert response.status_code == 400
    assert not WorkSchedule.objects.exists()


def test_another_tenants_week_is_not_listed(owner, salon, clinic, stylist,
                                            django_user_model):
    user = django_user_model.objects.create_user(email='x@example.com', password='pw')
    theirs = Membership.objects.create(user=user, tenant=clinic, attends_appointments=True)
    WorkSchedule.objects.create(
        tenant=clinic, professional=theirs, weekday=0,
        start_time=time(9), end_time=time(12),
    )
    WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(12),
    )

    rows = api(owner, salon).get(SCHEDULE_URL).json()

    assert len(rows) == 1


def test_the_week_can_be_narrowed_to_one_professional(owner, salon, stylist,
                                                      django_user_model):
    other_user = django_user_model.objects.create_user(email='b@example.com', password='pw')
    other = Membership.objects.create(user=other_user, tenant=salon, attends_appointments=True)
    WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(12),
    )
    WorkSchedule.objects.create(
        tenant=salon, professional=other, weekday=0,
        start_time=time(9), end_time=time(12),
    )

    rows = api(owner, salon).get(SCHEDULE_URL, {'professional': stylist.pk}).json()

    assert [row['professional'] for row in rows] == [stylist.pk]


def test_dropping_a_shift_takes_it_off_the_week(owner, salon, stylist):
    row = WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(12),
    )

    assert api(owner, salon).delete(schedule_detail(row)).status_code == 204
    assert not WorkSchedule.objects.exists()


# --- what comes out of the week ---------------------------------------------

def test_closing_the_whole_shop_names_nobody(owner, salon):
    start = datetime.combine(date(2099, 12, 25), time(0), tzinfo=ASUNCION)

    response = api(owner, salon).post(TIME_OFF_URL, {
        'start': start.isoformat(), 'end': (start + timedelta(days=1)).isoformat(),
        'reason': 'Navidad',
    }, format='json')

    assert response.status_code == 201
    assert TimeOff.objects.get().professional is None


def test_time_off_that_ends_before_it_starts_is_a_400(owner, salon, stylist):
    start = datetime.combine(date(2099, 6, 1), time(15), tzinfo=ASUNCION)

    response = api(owner, salon).post(TIME_OFF_URL, {
        'professional': stylist.pk,
        'start': start.isoformat(), 'end': (start - timedelta(hours=2)).isoformat(),
    }, format='json')

    assert response.status_code == 400
    assert 'end' in response.json()


def test_the_list_defaults_to_what_is_still_ahead(owner, salon, stylist):
    """A shop opening this wants the closures it has to plan around, and the
    past half of this table only ever grows."""
    from django.utils import timezone as dj_timezone
    now = dj_timezone.now()
    TimeOff.objects.create(
        tenant=salon, professional=stylist,
        start=now - timedelta(days=30), end=now - timedelta(days=29), reason='Old',
    )
    upcoming = TimeOff.objects.create(
        tenant=salon, professional=stylist,
        start=now + timedelta(days=1), end=now + timedelta(days=2), reason='Soon',
    )

    rows = api(owner, salon).get(TIME_OFF_URL).json()['results']

    assert [row['id'] for row in rows] == [upcoming.pk]


def test_an_explicit_range_reaches_the_past(owner, salon, stylist):
    from django.utils import timezone as dj_timezone
    now = dj_timezone.now()
    old = TimeOff.objects.create(
        tenant=salon, professional=stylist,
        start=now - timedelta(days=30), end=now - timedelta(days=29), reason='Old',
    )
    since = (now - timedelta(days=31)).date().isoformat()
    until = (now - timedelta(days=28)).date().isoformat()

    rows = api(owner, salon).get(TIME_OFF_URL, {'from': since, 'to': until}).json()['results']

    assert [row['id'] for row in rows] == [old.pk]


def test_a_malformed_date_is_a_400_not_a_500(owner, salon):
    response = api(owner, salon).get(TIME_OFF_URL, {'from': '01-06-2026'})

    assert response.status_code == 400
