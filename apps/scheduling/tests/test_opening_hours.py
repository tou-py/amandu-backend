"""
Booking is held to the professional's working week and their time off, on
every path a member books through: one turn, an edit, a series, a series move.
"""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service, TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

ZONE = ZoneInfo('America/Asuncion')
# A Monday in the future, so nothing here trips on "in the past".
MONDAY = datetime(2030, 9, 2, tzinfo=ZONE).date()
LIST = reverse('scheduling:appointment-list')
SERIES = reverse('scheduling:appointmentseries-list')


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=ZONE)


@pytest.fixture
def shop(db):
    return Tenant.objects.create(name='Shop', slug='shop', country='PY', timezone='America/Asuncion')


@pytest.fixture
def ana(django_user_model, shop):
    user = django_user_model.objects.create_user(email='ana@example.com', password='pw')
    ana = Membership.objects.create(user=user, tenant=shop, attends_appointments=True)
    # Monday 09-13 and 13-18 (back to back), nothing on Tuesday.
    WorkSchedule.objects.create(tenant=shop, professional=ana, weekday=0, start_time=time(9), end_time=time(13))
    WorkSchedule.objects.create(tenant=shop, professional=ana, weekday=0, start_time=time(13), end_time=time(18))
    return ana


@pytest.fixture
def cut(shop):
    return Service.objects.create(tenant=shop, name='Cut', duration=timedelta(hours=1))


@pytest.fixture
def ada(shop):
    return Client.objects.create(tenant=shop, name='Ada')


def http(membership):
    client = APIClient()
    client.force_authenticate(membership.user)
    client.credentials(HTTP_X_TENANT_ID=str(membership.tenant_id))
    return client


def book(membership, pro, service, client, start):
    return http(membership).post(
        LIST, {'professional': pro.pk, 'service': service.pk, 'clients': [str(client.pk)], 'start': start.isoformat()},
        format='json',
    )


def test_inside_the_week_books_even_across_back_to_back_stretches(ana, cut, ada):
    assert book(ana, ana, cut, ada, at(MONDAY, 12, 30)).status_code == 201


@pytest.mark.parametrize('start', [at(MONDAY, 8, 30), at(MONDAY, 17, 30), at(MONDAY + timedelta(days=1), 10)])
def test_outside_the_week_or_on_a_closed_day_is_refused(ana, cut, ada, start):
    res = book(ana, ana, cut, ada, start)

    assert (res.status_code, res.data['code']) == (400, 'outside_working_hours')
    assert 'start' in res.data


@pytest.mark.parametrize('whose', ['own', 'whole shop'])
def test_time_off_is_refused(shop, ana, cut, ada, whose):
    TimeOff.objects.create(
        tenant=shop, professional=ana if whose == 'own' else None, start=at(MONDAY, 10), end=at(MONDAY, 11),
    )

    res = book(ana, ana, cut, ada, at(MONDAY, 10, 30))

    assert (res.status_code, res.data['code']) == (400, 'time_off')


def test_no_schedule_means_no_hours_to_be_outside_of_but_time_off_still_counts(
    django_user_model, shop, cut, ada
):
    user = django_user_model.objects.create_user(email='bea@example.com', password='pw')
    bea = Membership.objects.create(user=user, tenant=shop, attends_appointments=True)
    TimeOff.objects.create(tenant=shop, start=at(MONDAY, 10), end=at(MONDAY, 11))

    assert book(bea, bea, cut, ada, at(MONDAY, 22)).status_code == 201
    assert book(bea, bea, cut, ada, at(MONDAY, 10)).data['code'] == 'time_off'


def test_rescheduling_out_of_hours_is_refused_but_editing_notes_is_not(shop, ana, cut, ada):
    created = book(ana, ana, cut, ada, at(MONDAY, 10))
    url = reverse('scheduling:appointment-detail', args=[created.data['id']])
    # The week shrinks under an existing turn: its notes must stay editable.
    WorkSchedule.objects.filter(professional=ana, start_time=time(9)).delete()

    assert http(ana).patch(url, {'notes': 'x'}, format='json').status_code == 200
    moved = http(ana).patch(url, {'start': at(MONDAY, 11).isoformat()}, format='json')
    assert (moved.status_code, moved.data['code']) == (400, 'outside_working_hours')


def series(ana, cut, ada, start, until):
    return http(ana).post(SERIES, {
        'professional': ana.pk, 'service': cut.pk, 'clients': [str(ada.pk)],
        'start': start.isoformat(), 'frequency': 'weekly', 'until': until.isoformat(),
    }, format='json')


def test_a_series_starting_out_of_hours_is_refused(ana, cut, ada):
    res = series(ana, cut, ada, at(MONDAY, 19), MONDAY + timedelta(weeks=3))

    assert (res.status_code, res.data['code']) == (400, 'outside_working_hours')
    assert not Appointment.objects.exists()


def test_a_series_skips_the_holiday(shop, ana, cut, ada):
    holiday = MONDAY + timedelta(weeks=1)
    TimeOff.objects.create(tenant=shop, start=at(holiday, 0), end=at(holiday + timedelta(days=1), 0))

    res = series(ana, cut, ada, at(MONDAY, 10), MONDAY + timedelta(weeks=2))

    assert res.status_code == 201
    assert res.data['skipped'] == [holiday.isoformat()]
    assert len(res.data['appointments']) == 2


def test_moving_a_series_out_of_hours_is_refused_and_a_holiday_is_left_behind(shop, ana, cut, ada):
    series(ana, cut, ada, at(MONDAY, 10), MONDAY + timedelta(weeks=2))
    first = Appointment.objects.order_by('start').first()
    url = reverse('scheduling:appointment-reschedule-following', args=[first.pk])

    refused = http(ana).post(url, {'time': '20:00'}, format='json')
    assert (refused.status_code, refused.data['code']) == (400, 'outside_working_hours')

    holiday = MONDAY + timedelta(weeks=1)
    TimeOff.objects.create(tenant=shop, professional=ana, start=at(holiday, 15), end=at(holiday, 16))
    res = http(ana).post(url, {'time': '15:00'}, format='json')
    assert res.status_code == 200
    assert res.data['skipped'] == [holiday.isoformat()]
