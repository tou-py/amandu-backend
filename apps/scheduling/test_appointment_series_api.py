from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, AppointmentSeries, Client, Service
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:appointmentseries-list')

# Chile still observes DST, and its spring transition falls on 2026-09-06: a
# Monday class at 07:00 is UTC-4 before it and UTC-3 after. That is exactly the
# case the roadmap flags as where recurring series break.
SANTIAGO = ZoneInfo('America/Santiago')
FIRST_MONDAY = datetime(2026, 8, 31, 7, 0, tzinfo=SANTIAGO)


def action_url(appointment, name):
    return reverse(f'scheduling:appointment-{name}', args=[appointment.pk])


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def studio(db):
    return Tenant.objects.create(
        name='Pilates', slug='pilates', country='CL', timezone='America/Santiago'
    )


@pytest.fixture
def other_studio(db):
    return Tenant.objects.create(name='Otro', slug='otro', country='CL')


@pytest.fixture
def receptionist(db, django_user_model, studio):
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=studio, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def teacher(db, django_user_model, studio):
    user = django_user_model.objects.create_user(email='t@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=studio, attends_appointments=True)


@pytest.fixture
def reformer(db, studio):
    return Service.objects.create(
        tenant=studio, name='Reformer', duration=timedelta(minutes=60)
    )


@pytest.fixture
def ada(db, studio):
    return Client.objects.create(tenant=studio, name='Ada')


def payload(teacher, reformer, ada, **overrides):
    body = {
        'professional': teacher.pk,
        'service': reformer.pk,
        'clients': [str(ada.pk)],
        'start': FIRST_MONDAY.isoformat(),
        'frequency': 'weekly',
        'interval': 1,
        'weekdays': [],
        'until': '2026-09-21',
    }
    body.update(overrides)
    return body


def local(appointment):
    return appointment.start.astimezone(SANTIAGO)


# --- booking the arrangement -------------------------------------------------

def test_a_series_books_every_occurrence_and_links_them(
    receptionist, studio, teacher, reformer, ada
):
    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert len(res.data['appointments']) == 4
    series = AppointmentSeries.objects.get()
    assert series.tenant == studio
    assert series.appointments.count() == 4
    # The roster travels to each one; a class of one is still a class.
    assert all(a.client_links.count() == 1 for a in series.appointments.all())


def test_the_wall_clock_time_survives_a_dst_transition(
    receptionist, studio, teacher, reformer, ada
):
    """
    Every occurrence is 07:00 for the person walking in, whatever the offset was
    that week. Adding seven days to a UTC instant would keep the instant and move
    the class, which is the bug this whole design exists to avoid.
    """
    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    booked = list(Appointment.objects.order_by('start'))
    assert [local(a).strftime('%H:%M') for a in booked] == ['07:00'] * 4
    # And the proof it is not a coincidence: the UTC instants are NOT a week
    # apart across the transition.
    assert [local(a).utcoffset() for a in booked[:2]] == [
        timedelta(hours=-4), timedelta(hours=-3),
    ]
    assert booked[1].start - booked[0].start == timedelta(days=7, hours=-1)


def test_two_weekdays_land_in_one_series(receptionist, studio, teacher, reformer, ada):
    res = api(receptionist, studio).post(
        LIST_URL,
        payload(teacher, reformer, ada, weekdays=[0, 2], until='2026-09-09'),
        format='json',
    )

    assert res.status_code == 201
    days = [local(a).date() for a in Appointment.objects.order_by('start')]
    assert days == [
        date(2026, 8, 31), date(2026, 9, 2), date(2026, 9, 7), date(2026, 9, 9),
    ]


def test_a_clash_skips_that_date_and_names_it(receptionist, studio, teacher, reformer, ada):
    """
    One busy Monday is not a reason to abandon a term. The receptionist is told
    which day to look at.
    """
    taken = datetime(2026, 9, 7, 7, 30, tzinfo=SANTIAGO)
    Appointment.objects.create(
        tenant=studio, professional=teacher, service=reformer,
        start=taken, end=taken + reformer.duration,
    )

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == ['2026-09-07']
    assert len(res.data['appointments']) == 3


def test_a_series_that_clashes_everywhere_is_refused_outright(
    receptionist, studio, teacher, reformer, ada
):
    """
    An empty arrangement is not a success anybody would read.

    Every blocker is at 07:30, half over the class and half not: a real clash,
    the one shape an enrolment can never absorb. On the hour it would be the
    same class and the series would join it instead.
    """
    for day in (31, 7, 14, 21):
        month = 8 if day == 31 else 9
        when = datetime(2026, month, day, 7, 30, tzinfo=SANTIAGO)
        Appointment.objects.create(
            tenant=studio, professional=teacher, service=reformer,
            start=when, end=when + reformer.duration,
        )

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 400
    assert AppointmentSeries.objects.count() == 0


def test_a_series_ending_before_it_starts_is_refused(
    receptionist, studio, teacher, reformer, ada
):
    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada, until='2026-08-01'), format='json'
    )

    assert res.status_code == 400
    assert 'until' in res.data


def test_a_run_too_long_to_book_is_refused_rather_than_silently_cut(
    receptionist, studio, teacher, reformer, ada
):
    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada, until='2099-01-01'), format='json'
    )

    assert res.status_code == 400
    assert 'until' in res.data
    assert Appointment.objects.count() == 0


def test_staff_cannot_book_a_series_for_somebody_else(
    db, django_user_model, studio, teacher, reformer, ada
):
    """Same rule as a single booking, which is why it lives in one place."""
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    Membership.objects.create(user=user, tenant=studio, attends_appointments=True)

    res = api(user, studio).post(LIST_URL, payload(teacher, reformer, ada), format='json')

    assert res.status_code == 400


def test_another_tenants_series_is_not_listed(receptionist, studio, other_studio):
    AppointmentSeries.objects.create(tenant=other_studio, until=date(2026, 12, 1))

    res = api(receptionist, studio).get(LIST_URL)

    assert res.status_code == 200
    assert res.data['results'] == []


# --- an occurrence that changes does not break the arrangement ---------------

def test_cancelling_one_occurrence_leaves_the_rest_alone(
    receptionist, studio, teacher, reformer, ada
):
    """The roadmap's hard requirement, and it is free: they are separate rows."""
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    second = Appointment.objects.order_by('start')[1]

    res = http.post(action_url(second, 'cancel'), {}, format='json')

    assert res.status_code == 200
    statuses = [a.status for a in Appointment.objects.order_by('start')]
    assert statuses == ['scheduled', 'cancelled', 'scheduled', 'scheduled']


def test_moving_one_occurrence_leaves_the_rest_alone(
    receptionist, studio, teacher, reformer, ada
):
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    second = Appointment.objects.order_by('start')[1]
    moved_to = datetime(2026, 9, 8, 18, 0, tzinfo=SANTIAGO)

    res = http.patch(
        reverse('scheduling:appointment-detail', args=[second.pk]),
        {'start': moved_to.isoformat()},
        format='json',
    )

    assert res.status_code == 200
    second.refresh_from_db()
    assert local(second) == moved_to
    assert second.series_id is not None, 'it is still part of the arrangement'
    assert Appointment.objects.filter(status='scheduled').count() == 4


# --- this one and the following ----------------------------------------------

def test_cancel_following_ends_the_run_from_here(receptionist, studio, teacher, reformer, ada):
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    third = Appointment.objects.order_by('start')[2]

    res = http.post(action_url(third, 'cancel-following'), {'reason': 'Se dio de baja'}, format='json')

    assert res.status_code == 200
    assert len(res.data) == 2
    statuses = [a.status for a in Appointment.objects.order_by('start')]
    assert statuses == ['scheduled', 'scheduled', 'cancelled', 'cancelled']
    assert Appointment.objects.filter(status='cancelled').first().cancellation_reason == 'Se dio de baja'


def test_cancel_following_tells_the_professional_once(
    receptionist, studio, teacher, reformer, ada
):
    """Forty rows saying the same thing is a bell nobody opens again."""
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    first = Appointment.objects.order_by('start')[0]

    http.post(action_url(first, 'cancel-following'), {}, format='json')

    assert Notification.objects.count() == 1


def test_cancel_following_needs_a_series(receptionist, studio, teacher, reformer):
    lone = Appointment.objects.create(
        tenant=studio, professional=teacher, service=reformer,
        start=FIRST_MONDAY, end=FIRST_MONDAY + reformer.duration,
    )

    res = api(receptionist, studio).post(action_url(lone, 'cancel-following'), {}, format='json')

    assert res.status_code == 400


def test_reschedule_following_moves_the_hour_and_keeps_each_date(
    receptionist, studio, teacher, reformer, ada
):
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    second = Appointment.objects.order_by('start')[1]

    res = http.post(action_url(second, 'reschedule-following'), {'time': '19:30'}, format='json')

    assert res.status_code == 200
    booked = list(Appointment.objects.order_by('start'))
    assert [local(a).strftime('%d %H:%M') for a in booked] == [
        '31 07:00',  # before the pivot, untouched
        '07 19:30', '14 19:30', '21 19:30',
    ]


def test_reschedule_following_keeps_the_local_hour_across_dst(
    receptionist, studio, teacher, reformer, ada
):
    """
    The pivot is before the transition and the rest after it. Every moved
    occurrence must read 19:30 locally, not 18:30 for the ones on the far side.
    """
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    first = Appointment.objects.order_by('start')[0]

    http.post(action_url(first, 'reschedule-following'), {'time': '19:30'}, format='json')

    assert {local(a).strftime('%H:%M') for a in Appointment.objects.all()} == {'19:30'}


def test_reschedule_following_leaves_a_clashing_occurrence_where_it_was(
    receptionist, studio, teacher, reformer, ada
):
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    blocked = datetime(2026, 9, 14, 19, 30, tzinfo=SANTIAGO)
    Appointment.objects.create(
        tenant=studio, professional=teacher, service=reformer,
        start=blocked, end=blocked + reformer.duration,
    )
    first = Appointment.objects.order_by('start')[0]

    res = http.post(action_url(first, 'reschedule-following'), {'time': '19:30'}, format='json')

    assert res.status_code == 200
    assert res.data['skipped'] == ['2026-09-14']
    stayed = Appointment.objects.filter(series__isnull=False).order_by('start')[2]
    assert local(stayed).strftime('%H:%M') == '07:00'


def test_reschedule_following_marks_every_occurrence_it_moved(
    receptionist, studio, teacher, reformer, ada
):
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    booked = list(Appointment.objects.order_by('start'))
    second = booked[1]
    was_at = {a.pk: a.start for a in booked}

    http.post(action_url(second, 'reschedule-following'), {'time': '19:30'}, format='json')

    moved = list(Appointment.objects.order_by('start'))
    assert moved[0].rescheduled_from is None  # before the pivot, never touched
    assert [a.rescheduled_from for a in moved[1:]] == [was_at[a.pk] for a in moved[1:]]


def test_reschedule_following_can_hand_the_run_to_someone_else(
    db, django_user_model, receptionist, studio, teacher, reformer, ada
):
    cover = Membership.objects.create(
        user=django_user_model.objects.create_user(email='c@example.com', password='pw'),
        tenant=studio,
        attends_appointments=True,
    )
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    second = Appointment.objects.order_by('start')[1]

    res = http.post(
        action_url(second, 'reschedule-following'),
        {'time': '07:00', 'professional': cover.pk},
        format='json',
    )

    assert res.status_code == 200
    handed = [a.professional_id for a in Appointment.objects.order_by('start')]
    assert handed == [teacher.pk, cover.pk, cover.pk, cover.pk]

def test_moving_a_run_puts_every_reminder_it_moved_back_in_the_queue(
    receptionist, studio, teacher, reformer, ada
):
    """
    The sharpest edge of the stamp, and the reason it is cleared at all: one
    POST moves a whole run, so an uncleared stamp would not lose one reminder
    but a term of them, on exactly the imminent dates where being wrong is
    worst. The occurrence before the pivot is untouched and must keep its own.
    """
    http = api(receptionist, studio)
    http.post(LIST_URL, payload(teacher, reformer, ada), format='json')
    booked = list(Appointment.objects.order_by('start'))
    sent = timezone.now()
    Appointment.objects.update(reminder_sent_at=sent)

    res = http.post(action_url(booked[1], 'reschedule-following'), {'time': '19:30'}, format='json')

    assert res.status_code == 200
    moved = list(Appointment.objects.order_by('start'))
    assert moved[0].reminder_sent_at == sent, 'the occurrence before the pivot never moved'
    assert [one.reminder_sent_at for one in moved[1:]] == [None, None, None]


# --- enrolling in a class that already exists --------------------------------

@pytest.fixture
def bob(db, studio):
    return Client.objects.create(tenant=studio, name='Bob')


@pytest.fixture
def barre(db, studio):
    return Service.objects.create(
        tenant=studio, name='Barre', duration=timedelta(minutes=60)
    )


def book_series(receptionist, studio, teacher, reformer, client, **overrides):
    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, client, **overrides), format='json'
    )
    assert res.status_code == 201, res.data
    return AppointmentSeries.objects.get(pk=res.data['id'])


def test_a_second_client_enrols_in_the_class_instead_of_being_turned_away(
    receptionist, studio, teacher, reformer, ada, bob
):
    """
    A recurring slot in a group business is ONE class. Bob wanting Ada's Monday
    is not a double booking, it is the second person on the mat.
    """
    book_series(receptionist, studio, teacher, reformer, ada)

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, bob), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == []
    assert res.data['joined'] == [
        '2026-08-31', '2026-09-07', '2026-09-14', '2026-09-21',
    ]
    assert len(res.data['appointments']) == 4
    # Nothing new in the diary, and Bob on the mat of every existing class.
    assert Appointment.objects.count() == 4
    assert all(a.client_links.count() == 2 for a in Appointment.objects.all())


def test_a_joined_class_keeps_its_own_series_and_the_roster_carries_the_new_one(
    receptionist, studio, teacher, reformer, ada, bob
):
    """
    The appointment belongs to whoever booked it; only the roster row belongs to
    the enrolment. Which is why the enrolment needs a series of its own -- the
    appointment's column is already answering somebody else's question.
    """
    ada_series = book_series(receptionist, studio, teacher, reformer, ada)

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, bob), format='json'
    )
    bob_series = AppointmentSeries.objects.get(pk=res.data['id'])

    assert list(bob_series.appointments.all()) == []
    assert ada_series.appointments.count() == 4
    assert bob_series.enrolments.count() == 4
    assert {link.client for link in bob_series.enrolments.all()} == {bob}
    assert {link.client for link in ada_series.enrolments.all()} == {ada}


def test_a_full_class_is_skipped_rather_than_squeezed(
    receptionist, studio, teacher, reformer, ada, bob
):
    """The class's own ceiling wins: enrolling in a room never widens it."""
    ada_series = book_series(receptionist, studio, teacher, reformer, ada)
    full = ada_series.appointments.get(start=datetime(2026, 9, 7, 7, 0, tzinfo=SANTIAGO))
    full.capacity = 1
    full.save(update_fields=['capacity'])

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, bob), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == ['2026-09-07']
    assert res.data['joined'] == ['2026-08-31', '2026-09-14', '2026-09-21']
    full.refresh_from_db()
    assert full.capacity == 1
    assert [link.client for link in full.client_links.all()] == [ada]


def test_another_service_at_the_same_hour_is_a_clash_not_a_class(
    receptionist, studio, teacher, reformer, barre, ada, bob
):
    """Two different things cannot happen in the same room at once."""
    taken = datetime(2026, 9, 7, 7, 0, tzinfo=SANTIAGO)
    other = Appointment.objects.create(
        tenant=studio, professional=teacher, service=barre,
        start=taken, end=taken + barre.duration,
    )

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == ['2026-09-07']
    assert res.data['joined'] == []
    assert other.client_links.count() == 0


def test_an_appointment_that_only_partly_overlaps_is_never_joined(
    receptionist, studio, teacher, reformer, ada
):
    """
    Half over the class is the appointment next door running long, not the class
    itself. Enrolling into it would put the client somewhere they never asked to
    be, at an hour that is not the one on their card.
    """
    taken = datetime(2026, 9, 7, 7, 30, tzinfo=SANTIAGO)
    overlapping = Appointment.objects.create(
        tenant=studio, professional=teacher, service=reformer,
        start=taken, end=taken + reformer.duration,
    )

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == ['2026-09-07']
    assert res.data['joined'] == []
    assert overlapping.client_links.count() == 0


def test_a_cancelled_class_neither_blocks_the_hour_nor_absorbs_the_enrolment(
    receptionist, studio, teacher, reformer, ada, bob
):
    """A called-off class is not one anybody can still walk into."""
    when = datetime(2026, 9, 7, 7, 0, tzinfo=SANTIAGO)
    called_off = Appointment.objects.create(
        tenant=studio, professional=teacher, service=reformer,
        start=when, end=when + reformer.duration,
        status=Appointment.Status.CANCELLED,
    )

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == []
    assert res.data['joined'] == []
    assert len(res.data['appointments']) == 4
    assert called_off.client_links.count() == 0


def test_a_series_that_joins_every_date_is_still_a_complete_success(
    receptionist, studio, teacher, reformer, ada, bob
):
    """
    Nothing created is not nothing done. The client is enrolled in every class
    they asked for, which is the entire point of asking.
    """
    book_series(receptionist, studio, teacher, reformer, ada)

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, bob), format='json'
    )

    assert res.status_code == 201
    assert AppointmentSeries.objects.count() == 2
    assert AppointmentSeries.objects.get(pk=res.data['id']).enrolments.count() == 4


def test_enrolling_somebody_already_in_the_class_does_not_duplicate_them(
    receptionist, studio, teacher, reformer, ada
):
    """
    The end state asked for already holds, so the join succeeded. Inserting Ada
    twice would only break the roster's unique constraint and take the other
    dates down with it.
    """
    book_series(receptionist, studio, teacher, reformer, ada)

    res = api(receptionist, studio).post(
        LIST_URL, payload(teacher, reformer, ada), format='json'
    )

    assert res.status_code == 201
    assert res.data['skipped'] == []
    assert len(res.data['joined']) == 4
    assert all(a.client_links.count() == 1 for a in Appointment.objects.all())
