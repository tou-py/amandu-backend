from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.models import ProtectedError
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service
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
    """Acts for the tenant but does not attend appointments (attends default False)."""
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
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
    appointments. Fails if `clients` stops being prefetched."""
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
    res = api(receptionist, salon).get(LIST_URL, {'status': 'pending'})

    assert res.status_code == 400


def test_another_tenants_appointment_is_not_reachable(receptionist, salon, stylist, haircut, clinic):
    foreign = Appointment.objects.create(
        tenant=clinic, professional=stylist, service=haircut,
        start=TOMORROW, end=TOMORROW + timedelta(minutes=30),
    )

    res = api(receptionist, salon).get(detail_url(foreign))

    assert res.status_code == 404
