from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, Client, Service
from apps.scheduling.serializers import AppointmentSerializer
from apps.scheduling.views import AppointmentViewSet
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:appointment-list')
TOMORROW = timezone.now().replace(microsecond=0) + timedelta(days=1)


def detail_url(appointment):
    return reverse('scheduling:appointment-detail', args=[appointment.pk])


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
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def receptionist(db, django_user_model, salon):
    """
    Books the team's diary but does not attend appointments (attends default
    False). Coordinator, not staff: booking someone else's day is what this
    fixture exists to do, and staff may only book their own.
    """
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def client_(db, salon):
    return Client.objects.create(tenant=salon, name='Ada')


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


def booking(professional, clients, service, start):
    return {
        'professional': professional.pk,
        'clients': [c.pk for c in clients],
        'service': service.pk,
        'start': start.isoformat(),
    }


def test_booking_assigns_tenant_and_derives_end(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201
    appointment = Appointment.objects.get(pk=res.data['id'])
    assert appointment.tenant == salon
    assert appointment.end == TOMORROW + timedelta(minutes=30)
    assert appointment.status == Appointment.Status.SCHEDULED


def test_a_slot_can_hold_a_group_of_clients(receptionist, salon, stylist, client_, haircut):
    bob = Client.objects.create(tenant=salon, name='Bob')

    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_, bob], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201
    assert Appointment.objects.get(pk=res.data['id']).clients.count() == 2


def test_an_appointment_needs_at_least_one_client(receptionist, salon, stylist, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 400
    assert 'clients' in res.data


def test_overlap_for_the_same_professional_is_rejected(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')

    res = http.post(
        LIST_URL,
        booking(stylist, [client_], haircut, TOMORROW + timedelta(minutes=15)),
        format='json',
    )

    assert res.status_code == 400


def test_a_clash_that_beats_the_python_check_is_a_409_not_a_500(
    receptionist, salon, stylist, client_, haircut,
):
    """
    The race the serializer cannot close: it checks, finds the slot free, and by
    the time it inserts somebody else has taken it. Reproduced deterministically
    by letting validate() see nothing -- which is exactly what it sees while the
    competing INSERT is still uncommitted -- with the row really there.

    409, not 500: the request was valid when it was made.
    """
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    # Everything validate() does except look for the clash. `end` still has to
    # be derived here: it is read-only on the way in and the model requires it.
    blind = lambda self, attrs: {**attrs, 'end': attrs['start'] + attrs['service'].duration}  # noqa: E731

    with mock.patch.object(AppointmentSerializer, 'validate', blind):
        res = api(receptionist, salon).post(
            LIST_URL,
            booking(stylist, [client_], haircut, TOMORROW + timedelta(minutes=15)),
            format='json',
        )

    assert res.status_code == 409
    # The wording the frontend matches on, shared with the serializer's own 400.
    assert 'already has an appointment' in str(res.data)
    assert Appointment.objects.count() == 1


def test_an_unrelated_integrity_error_still_surfaces_as_a_fault(db):
    """
    The guard is by constraint name. Anything else failing on the way in is a
    real bug and must not be dressed up as an ordinary scheduling clash.
    """
    def save(_serializer):
        raise IntegrityError('duplicate key value violates unique constraint "something_else"')

    with pytest.raises(IntegrityError):
        AppointmentViewSet._save_or_conflict(save, None)


def test_back_to_back_appointments_are_allowed(receptionist, salon, stylist, client_, haircut):
    """'[)' bounds: one ending exactly when the next begins do not overlap."""
    http = api(receptionist, salon)
    http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')

    res = http.post(
        LIST_URL,
        booking(stylist, [client_], haircut, TOMORROW + timedelta(minutes=30)),
        format='json',
    )

    assert res.status_code == 201


def test_two_professionals_may_share_a_time(receptionist, salon, stylist, client_, haircut, django_user_model):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='o@example.com', password='pw'),
        tenant=salon,
        attends_appointments=True,
    )
    http = api(receptionist, salon)
    http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')

    res = http.post(LIST_URL, booking(other, [client_], haircut, TOMORROW), format='json')

    assert res.status_code == 201


def test_a_cancelled_appointment_frees_the_slot(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    first = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    http.post(action_url(Appointment.objects.get(pk=first.data['id']), 'cancel'))

    res = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')

    assert res.status_code == 201


def test_a_non_attending_membership_cannot_be_the_professional(receptionist, salon, client_, haircut):
    """The receptionist has attends_appointments=False, so their own membership
    is not a valid professional."""
    caller = Membership.objects.get(user__email='r@example.com', tenant=salon)

    res = api(receptionist, salon).post(
        LIST_URL, booking(caller, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 400
    assert 'professional' in res.data


def test_a_foreign_tenants_client_cannot_be_booked(receptionist, salon, stylist, haircut, clinic):
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [foreign], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 400
    assert 'clients' in res.data


def test_a_booked_client_cannot_be_deleted(salon, stylist, client_, haircut):
    """The through FK is PROTECT: a client with appointments keeps its history."""
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    appointment.clients.add(client_)

    with pytest.raises(ProtectedError):
        client_.delete()


def test_deleting_a_booked_client_is_a_conflict_not_a_crash(
    receptionist, salon, stylist, client_, haircut
):
    """
    The ORM guard above is correct but was reaching the API as an unhandled
    ProtectedError, i.e. a 500: an operator's ordinary mistake reported as a
    server fault, with nothing in it to act on.
    """
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    appointment.clients.add(client_)

    res = api(receptionist, salon).delete(
        reverse('scheduling:client-detail', args=[client_.pk])
    )

    assert res.status_code == 409
    assert Client.objects.filter(pk=client_.pk).exists()


def test_deleting_a_service_in_use_is_a_conflict_not_a_crash(
    receptionist, salon, stylist, client_, haircut
):
    """Same defect, different resource: the guard belongs to the shared base."""
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    appointment.clients.add(client_)

    res = api(receptionist, salon).delete(
        reverse('scheduling:service-detail', args=[haircut.pk])
    )

    assert res.status_code == 409
    assert Service.objects.filter(pk=haircut.pk).exists()


def test_an_unreferenced_client_still_deletes(receptionist, salon, client_):
    """The guard must only fire on real references."""
    res = api(receptionist, salon).delete(
        reverse('scheduling:client-detail', args=[client_.pk])
    )

    assert res.status_code == 204
    assert not Client.objects.filter(pk=client_.pk).exists()


def test_cancel_keeps_the_row_visible_with_a_reason(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(action_url(appointment, 'cancel'), {'reason': 'client called off'}, format='json')

    assert res.status_code == 200
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.CANCELLED
    assert appointment.cancelled_at is not None
    assert appointment.cancellation_reason == 'client called off'
    assert Appointment.objects.filter(pk=appointment.pk).exists()


def test_a_cancellation_reason_must_be_text(receptionist, salon, stylist, client_, haircut):
    """`reason` is free text that lands in the record, so it goes through a field
    like any other input instead of being read raw off request.data."""
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(action_url(appointment, 'cancel'), {'reason': ['not', 'text']}, format='json')

    assert res.status_code == 400
    assert 'reason' in res.data
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.SCHEDULED


def test_a_terminal_appointment_cannot_be_cancelled_again(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])
    http.post(action_url(appointment, 'cancel'))

    res = http.post(action_url(appointment, 'cancel'))

    assert res.status_code == 400


def test_cancelling_a_teammates_appointment_notifies_them(
    receptionist, salon, stylist, client_, haircut
):
    """The sibling of OwnsAppointmentOrActsForTheTeam: a coordinator may act on
    a colleague's slot, and this is what tells the colleague it happened."""
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    http.post(action_url(appointment, 'cancel'))

    notification = Notification.objects.get()
    caller = Membership.objects.get(user__email='r@example.com', tenant=salon)
    assert notification.recipient == stylist
    assert notification.actor == caller
    assert notification.verb == Notification.Verb.APPOINTMENT_CANCELLED


def test_cancelling_your_own_appointment_does_not_notify_you(salon, stylist, client_, haircut):
    http = api(stylist.user, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    http.post(action_url(appointment, 'cancel'))

    assert not Notification.objects.exists()


def test_a_future_appointment_cannot_be_completed(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(action_url(appointment, 'complete'))

    assert res.status_code == 400


def test_a_past_appointment_can_be_completed(receptionist, salon, stylist, client_, haircut):
    past = timezone.now() - timedelta(hours=2)
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=past, end=past + timedelta(minutes=30),
    )
    appointment.clients.add(client_)

    res = api(receptionist, salon).post(action_url(appointment, 'complete'))

    assert res.status_code == 200
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.COMPLETED


def test_a_new_booking_starts_with_everyone_pending(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201
    assert [a['attendance'] for a in res.data['attendees']] == ['pending']


def test_attendance_is_recorded_per_person(receptionist, salon, stylist, client_, haircut):
    """The whole reason the fact moved off the appointment: one absence in a
    group says nothing about the people who did turn up."""
    bob = Client.objects.create(tenant=salon, name='Bob')
    http = api(receptionist, salon)
    created = http.post(
        LIST_URL, booking(stylist, [client_, bob], haircut, TOMORROW), format='json'
    )
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(client_.pk), 'attendance': 'no_show'},
        format='json',
    )

    assert res.status_code == 200
    recorded = {a['name']: a['attendance'] for a in res.data['attendees']}
    assert recorded == {'Ada': 'no_show', 'Bob': 'pending'}
    # The booking itself still only says what happened to the booking.
    assert res.data['status'] == Appointment.Status.SCHEDULED


def test_marking_attendance_does_not_reorder_the_roster(
    receptionist, salon, stylist, client_, haircut
):
    """People scan the roster positionally ("the second one is always Maria"),
    so recording one person's attendance must never reshuffle the others --
    an UPDATE to one through row is not license for an unordered SELECT to
    hand back a different order on the next read.

    Whichever order the roster is created in is not asserted here -- that
    order is a separate concern (see AppointmentClient.Meta.ordering) -- only
    that marking attendance, and any read after it, preserves it exactly."""
    bob = Client.objects.create(tenant=salon, name='Bob')
    mia = Client.objects.create(tenant=salon, name='Mia')
    http = api(receptionist, salon)
    created = http.post(
        LIST_URL, booking(stylist, [client_, bob, mia], haircut, TOMORROW), format='json'
    )
    appointment = Appointment.objects.get(pk=created.data['id'])
    original_order = [a['name'] for a in created.data['attendees']]
    assert set(original_order) == {'Ada', 'Bob', 'Mia'}

    # Mark the client that is NOT first in the established order, whichever
    # one that is -- marking the first would not exercise the reorder bug.
    middle = next(name for name in original_order if name != original_order[0])
    middle_pk = {'Ada': client_.pk, 'Bob': bob.pk, 'Mia': mia.pk}[middle]
    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(middle_pk), 'attendance': 'attended'},
        format='json',
    )

    assert res.status_code == 200
    assert [a['name'] for a in res.data['attendees']] == original_order

    refetched = http.get(detail_url(appointment))
    assert [a['name'] for a in refetched.data['attendees']] == original_order


def test_a_late_cancellation_is_no_longer_a_verdict_of_its_own(
    receptionist, salon, stylist, client_, haircut
):
    """
    The shops never charged it differently from a silent absence, so it went.
    Anything still sending the old value gets told, rather than having it
    quietly stored as something else.
    """
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(client_.pk), 'attendance': 'late_cancel'},
        format='json',
    )

    assert res.status_code == 400
    assert 'attendance' in res.data


def test_a_client_outside_the_appointment_cannot_be_marked(
    receptionist, salon, stylist, client_, haircut
):
    stranger = Client.objects.create(tenant=salon, name='Stranger')
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(stranger.pk), 'attendance': 'attended'},
        format='json',
    )

    assert res.status_code == 400
    assert 'client' in res.data


def test_a_cancelled_booking_has_no_attendance_to_record(
    receptionist, salon, stylist, client_, haircut
):
    """It never ran, so nobody in it attended or failed to."""
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])
    http.post(action_url(appointment, 'cancel'))

    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(client_.pk), 'attendance': 'attended'},
        format='json',
    )

    assert res.status_code == 400
    assert appointment.client_links.get().attendance == 'pending'


def test_an_unknown_attendance_value_is_rejected(receptionist, salon, stylist, client_, haircut):
    http = api(receptionist, salon)
    created = http.post(LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json')
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.post(
        action_url(appointment, 'attendance'),
        {'client': str(client_.pk), 'attendance': 'maybe'},
        format='json',
    )

    assert res.status_code == 400
    assert 'attendance' in res.data


def test_the_database_itself_forbids_an_overlap(salon, stylist, client_, haircut):
    """The ExclusionConstraint, not the serializer, is the real guarantee: a
    direct insert bypassing every check still cannot double-book."""
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Appointment.objects.create(
                tenant=salon, professional=stylist, service=haircut,
                start=TOMORROW + timedelta(minutes=15),
                end=TOMORROW + timedelta(minutes=45),
            )


def test_appointment_ids_are_uuid7(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert Appointment.objects.get(pk=res.data['id']).id.version == 7


def test_listing_appointments_does_not_scale_queries(receptionist, salon, stylist, haircut):
    """N+1 guard: the query count for the list must not grow with the number of
    appointments. Fails if `client_links__client` stops being prefetched."""
    ada = Client.objects.create(tenant=salon, name='Ada')
    bob = Client.objects.create(tenant=salon, name='Bob')

    def book(offset_minutes):
        start = TOMORROW + timedelta(minutes=offset_minutes)
        appointment = Appointment.objects.create(
            tenant=salon, professional=stylist, service=haircut,
            start=start, end=start + timedelta(minutes=30),
        )
        appointment.clients.add(ada, bob)

    http = api(receptionist, salon)

    book(0)
    with CaptureQueriesContext(connection) as one_appointment:
        http.get(LIST_URL)

    book(30)
    book(60)
    with CaptureQueriesContext(connection) as three_appointments:
        http.get(LIST_URL)

    assert len(three_appointments) == len(one_appointment)


def test_the_list_is_paginated(receptionist, salon, stylist, haircut):
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(receptionist, salon).get(LIST_URL)

    assert res.status_code == 200
    assert res.data['count'] == 1
    assert 'results' in res.data


def test_the_list_answers_304_when_nothing_changed_since(receptionist, salon, stylist, haircut):
    """
    ETag/If-None-Match round trip: a mobile client polling the agenda should get
    a cheap 304 instead of the full page when nothing changed. Sent with
    Accept-Encoding: gzip, as a browser does -- GZipMiddleware downgrades the
    ETag to weak, and the round trip has to survive that.
    """
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    http = api(receptionist, salon)

    first = http.get(LIST_URL, HTTP_ACCEPT_ENCODING='gzip')
    assert first.status_code == 200
    assert first.headers.get('ETag')
    assert first.headers['Cache-Control'] == 'private, no-cache'

    second = http.get(
        LIST_URL, HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH=first.headers['ETag'],
    )
    assert second.status_code == 304


def test_a_reschedule_in_the_same_second_is_still_visible(receptionist, salon, stylist, haircut):
    """
    The regression that a Last-Modified validator could not express. HTTP dates
    have one-second resolution, so a move landing in the same second as the
    previous poll used to hash to an identical validator and come back 304 --
    and stay 304, because a 304 never advances what the client stored. Dragging
    a block moments after any other edit is exactly that window.
    """
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    http = api(receptionist, salon)

    first = http.get(LIST_URL, HTTP_ACCEPT_ENCODING='gzip')
    moved = TOMORROW + timedelta(hours=3)
    assert http.patch(
        detail_url(appointment), {'start': moved.isoformat()}, format='json',
    ).status_code == 200

    # Both validators the browser stored, replayed together, with no clock
    # advanced in between: the response must carry the new start, not a 304.
    second = http.get(
        LIST_URL,
        HTTP_ACCEPT_ENCODING='gzip',
        HTTP_IF_NONE_MATCH=first.headers['ETag'],
        HTTP_IF_MODIFIED_SINCE=first.headers.get('Last-Modified', ''),
    )
    assert second.status_code == 200
    assert datetime.fromisoformat(second.data['results'][0]['start']) == moved


def test_filter_by_professional(receptionist, salon, stylist, haircut, django_user_model):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='o2@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    mine = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(receptionist, salon).get(LIST_URL, {'professional': stylist.pk})

    assert [a['id'] for a in res.data['results']] == [str(mine.pk)]


def test_filter_by_status(receptionist, salon, stylist, haircut):
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    done = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW + timedelta(hours=1), end=TOMORROW + timedelta(hours=1, minutes=30),
        status=Appointment.Status.COMPLETED,
    )

    res = api(receptionist, salon).get(LIST_URL, {'status': 'completed'})

    assert [a['id'] for a in res.data['results']] == [str(done.pk)]


def test_date_filter_honors_the_tenant_timezone(db, django_user_model):
    """R13: 'from'/'to' are days in the tenant timezone, not UTC. An appointment
    at 02:00 UTC belongs to the previous day in Buenos Aires (UTC-3)."""
    ba = Tenant.objects.create(
        name='BA', slug='ba', timezone='America/Argentina/Buenos_Aires'
    )
    caller = django_user_model.objects.create_user(email='ba@example.com', password='pw')
    Membership.objects.create(user=caller, tenant=ba)
    pro = Membership.objects.create(
        user=django_user_model.objects.create_user(email='pro@example.com', password='pw'),
        tenant=ba, attends_appointments=True,
    )
    service = Service.objects.create(tenant=ba, name='Cut', duration=timedelta(minutes=30))
    start = datetime(2026, 6, 1, 2, 0, tzinfo=ZoneInfo('UTC'))
    Appointment.objects.create(
        tenant=ba, professional=pro, service=service,
        start=start, end=start + timedelta(minutes=30),
    )
    http = api(caller, ba)

    local_day = http.get(LIST_URL, {'from': '2026-05-31', 'to': '2026-05-31'})
    utc_day = http.get(LIST_URL, {'from': '2026-06-01', 'to': '2026-06-01'})

    assert local_day.data['count'] == 1
    assert utc_day.data['count'] == 0


def test_a_malformed_date_filter_is_rejected(receptionist, salon):
    res = api(receptionist, salon).get(LIST_URL, {'from': '01-06-2026'})

    assert res.status_code == 400


def test_an_unknown_status_filter_is_rejected(receptionist, salon):
    # Not 'pending': that used to be the example of a status the domain does not
    # have, and the public booking page gave it a meaning.
    res = api(receptionist, salon).get(LIST_URL, {'status': 'rescheduled'})

    assert res.status_code == 400


def test_a_booking_records_who_made_it(receptionist, salon, stylist, haircut, client_):
    """Recorded by the server, never taken from the payload: a body that could
    set these could dress a public request up as a staff booking."""
    res = api(receptionist, salon).post(LIST_URL, {
        'professional': stylist.pk, 'service': haircut.pk,
        'clients': [str(client_.pk)], 'start': TOMORROW.isoformat(),
        'source': 'public', 'created_by': None,
    }, format='json')

    assert res.status_code == 201
    appointment = Appointment.objects.get()
    assert appointment.source == Appointment.Source.STAFF
    assert appointment.created_by.user == receptionist


def test_the_shop_can_filter_the_requests_waiting_on_it(receptionist, salon, stylist,
                                                        haircut):
    """The queue the shop actually works from: what the public page asked for
    and nobody has answered yet."""
    waiting = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
        status=Appointment.Status.PENDING, source=Appointment.Source.PUBLIC,
    )
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW + timedelta(hours=2), end=TOMORROW + timedelta(hours=2, minutes=30),
    )

    res = api(receptionist, salon).get(LIST_URL, {'status': 'pending'})

    assert [row['id'] for row in res.json()['results']] == [str(waiting.pk)]


def test_another_tenants_appointment_is_not_reachable(receptionist, salon, stylist, haircut, clinic):
    foreign = Appointment.objects.create(
        tenant=clinic, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(receptionist, salon).get(detail_url(foreign))

    assert res.status_code == 404


def test_staff_may_book_their_own_day(salon, stylist, client_, haircut):
    """The one professional a staff member is accountable for is themselves."""
    res = api(stylist.user, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201


def test_staff_may_not_book_a_colleagues_day(db, django_user_model, salon, stylist, client_, haircut):
    """
    Booking for a colleague fills THEIR day, which they answer for. It takes a
    role that answers for the diary as a whole: owner, admin or coordinator.
    """
    user = django_user_model.objects.create_user(email='other@example.com', password='pw')
    other = Membership.objects.create(user=user, tenant=salon, attends_appointments=True)

    res = api(other.user, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 400
    assert 'professional' in res.data
    assert not Appointment.objects.exists()


def test_a_coordinator_may_book_for_the_whole_team(receptionist, salon, stylist, client_, haircut):
    """Coordinator attends clients like staff but keeps the team's diary."""
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201


def test_staff_may_not_reassign_an_appointment_to_someone_else(
    db, django_user_model, salon, stylist, client_, haircut
):
    """The rule lives in the serializer, so a PATCH cannot walk around it."""
    user = django_user_model.objects.create_user(email='other2@example.com', password='pw')
    other = Membership.objects.create(user=user, tenant=salon, attends_appointments=True)
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(other.user, salon).patch(
        detail_url(appointment), {'professional': stylist.pk}, format='json'
    )

    assert res.status_code == 400
    appointment.refresh_from_db()
    assert appointment.professional == other


def test_staff_may_not_reschedule_a_colleagues_appointment(
    db, django_user_model, salon, stylist, client_, haircut
):
    """
    validate_professional never runs here: a drag-to-reschedule PATCH sends
    only `start`, so the old field-level check had nothing to look at. This is
    the object-level permission's job instead.
    """
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other3@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(stylist.user, salon).patch(
        detail_url(appointment), {'start': (TOMORROW + timedelta(hours=1)).isoformat()},
        format='json',
    )

    assert res.status_code == 403
    appointment.refresh_from_db()
    assert appointment.start == TOMORROW


def test_staff_may_reschedule_their_own_appointment(salon, stylist, client_, haircut):
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    new_start = TOMORROW + timedelta(hours=1)

    res = api(stylist.user, salon).patch(
        detail_url(appointment), {'start': new_start.isoformat()}, format='json'
    )

    assert res.status_code == 200
    appointment.refresh_from_db()
    assert appointment.start == new_start


def test_a_coordinator_may_reschedule_anyones_appointment(receptionist, salon, stylist, haircut):
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    new_start = TOMORROW + timedelta(hours=1)

    res = api(receptionist, salon).patch(
        detail_url(appointment), {'start': new_start.isoformat()}, format='json'
    )

    assert res.status_code == 200


def test_staff_may_not_cancel_a_colleagues_appointment(
    db, django_user_model, salon, stylist, client_, haircut
):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other4@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(stylist.user, salon).post(action_url(appointment, 'cancel'))

    assert res.status_code == 403
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.SCHEDULED


def test_staff_may_not_complete_a_colleagues_appointment(
    db, django_user_model, salon, stylist, client_, haircut
):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other5@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    past = timezone.now() - timedelta(hours=2)
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=past, end=past + timedelta(minutes=30),
    )

    res = api(stylist.user, salon).post(action_url(appointment, 'complete'))

    assert res.status_code == 403
    appointment.refresh_from_db()
    assert appointment.status == Appointment.Status.SCHEDULED


def test_staff_may_not_mark_attendance_for_a_colleagues_appointment(
    db, django_user_model, salon, stylist, client_, haircut
):
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other6@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )
    appointment.clients.add(client_)

    res = api(stylist.user, salon).post(
        action_url(appointment, 'attendance'),
        {'client': str(client_.pk), 'attendance': 'attended'},
        format='json',
    )

    assert res.status_code == 403
    assert appointment.client_links.get().attendance == 'pending'


def test_a_colleague_may_still_read_the_appointment(
    db, django_user_model, salon, stylist, client_, haircut
):
    """The shared calendar is unaffected: only writes are scoped."""
    other = Membership.objects.create(
        user=django_user_model.objects.create_user(email='other7@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    appointment = Appointment.objects.create(
        tenant=salon, professional=other, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(stylist.user, salon).get(detail_url(appointment))

    assert res.status_code == 200


def test_a_booking_defaults_to_five_places(receptionist, salon, stylist, client_, haircut):
    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, [client_], haircut, TOMORROW), format='json'
    )

    assert res.status_code == 201
    assert res.data['capacity'] == 5


def test_a_roster_over_capacity_is_refused(receptionist, salon, stylist, haircut):
    people = [Client.objects.create(tenant=salon, name=f'P{i}') for i in range(6)]

    res = api(receptionist, salon).post(
        LIST_URL, booking(stylist, people, haircut, TOMORROW), format='json'
    )

    assert res.status_code == 400
    assert 'holds 5 people and 6 were booked' in str(res.data)
    assert Appointment.objects.count() == 0


def test_capacity_can_be_raised_to_fit_a_bigger_group(receptionist, salon, stylist, haircut):
    people = [Client.objects.create(tenant=salon, name=f'P{i}') for i in range(6)]

    res = api(receptionist, salon).post(
        LIST_URL,
        {**booking(stylist, people, haircut, TOMORROW), 'capacity': 8},
        format='json',
    )

    assert res.status_code == 201
    assert Appointment.objects.get(pk=res.data['id']).clients.count() == 6


def test_capacity_cannot_be_lowered_under_the_people_already_booked(
    receptionist, salon, stylist, haircut,
):
    """The other direction of the same rule. A PATCH sending only `capacity`
    never touches `clients`, so the count has to come off the instance."""
    people = [Client.objects.create(tenant=salon, name=f'P{i}') for i in range(4)]
    http = api(receptionist, salon)
    created = http.post(
        LIST_URL, booking(stylist, people, haircut, TOMORROW), format='json'
    )
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.patch(detail_url(appointment), {'capacity': 2}, format='json')

    assert res.status_code == 400
    assert 'holds 2 people and 4 were booked' in str(res.data)
    appointment.refresh_from_db()
    assert appointment.capacity == 5


def test_adding_one_person_too_many_to_an_existing_slot_is_refused(
    receptionist, salon, stylist, haircut,
):
    people = [Client.objects.create(tenant=salon, name=f'P{i}') for i in range(6)]
    http = api(receptionist, salon)
    created = http.post(
        LIST_URL, booking(stylist, people[:5], haircut, TOMORROW), format='json'
    )
    appointment = Appointment.objects.get(pk=created.data['id'])

    res = http.patch(
        detail_url(appointment),
        {'clients': [str(c.pk) for c in people]},
        format='json',
    )

    assert res.status_code == 400
    assert appointment.clients.count() == 5
