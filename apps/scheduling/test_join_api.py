"""
Joining a turn into another of the same professional: the answer to a
reschedule onto an hour that is already taken (AppointmentViewSet.join).
"""
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, AppointmentClient, Client, Service
from apps.tenancy.models import Tenant

THREE = timezone.now().replace(microsecond=0) + timedelta(days=1)
FIVE = THREE + timedelta(hours=2)


def join(user, tenant, source, into, clients, reason=''):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    body = {'into': str(into.pk), 'clients': [c.pk for c in clients], 'reason': reason}
    return http.post(reverse('scheduling:appointment-join', args=[source.pk]), body, format='json')


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY', addon_cobros=True)


@pytest.fixture
def desk(django_user_model, salon):
    user = django_user_model.objects.create_user(email='desk@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def lorena(django_user_model, salon):
    user = django_user_model.objects.create_user(email='lorena@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def consulta(salon):
    return Service.objects.create(tenant=salon, name='Consulta', duration=timedelta(hours=1), price=150_000)


@pytest.fixture
def sesion(salon):
    return Service.objects.create(tenant=salon, name='Sesión', duration=timedelta(hours=1), price=180_000)


@pytest.fixture
def people(salon):
    return [Client.objects.create(tenant=salon, name=name) for name in ('Ana', 'Belén', 'Carolina')]


def turn(salon, professional, service, start, clients, capacity=3, status=Appointment.Status.SCHEDULED):
    appointment = Appointment.objects.create(
        tenant=salon, professional=professional, service=service, start=start,
        end=start + service.duration, capacity=capacity, status=status,
    )
    for client in clients:
        AppointmentClient.objects.create(appointment=appointment, client=client)
    return appointment


def paid(salon, appointment, client):
    return CashEntry.objects.create(
        tenant=salon, kind='income', amount=appointment.service.price, occurred_on=THREE.date(),
        concept='turn', appointment=appointment, client=client,
    )


def roster(appointment):
    return set(appointment.clients.values_list('name', flat=True))


def test_the_chosen_clients_move_and_the_rest_stay(desk, salon, lorena, consulta, people):
    ana, belen, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana, belen])
    into = turn(salon, lorena, consulta, FIVE, [carolina])

    res = join(desk, salon, source, into, [ana])

    assert res.status_code == 200
    assert res.data['id'] == str(into.pk)
    assert roster(into) == {'Ana', 'Carolina'}
    assert roster(source) == {'Belén'}
    source.refresh_from_db()
    assert source.status == Appointment.Status.SCHEDULED


def test_a_turn_left_empty_is_cancelled_with_the_reason(desk, salon, lorena, consulta, sesion, people):
    ana, belen, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana, belen])
    into = turn(salon, lorena, sesion, FIVE, [carolina])

    res = join(desk, salon, source, into, [ana, belen], reason='Reagendado al turno de las 17:00')

    assert res.status_code == 200
    source.refresh_from_db()
    assert source.status == Appointment.Status.CANCELLED
    assert source.cancellation_reason == 'Reagendado al turno de las 17:00'
    assert roster(into) == {'Ana', 'Belén', 'Carolina'}


def test_a_payment_goes_with_its_client_on_the_same_service(desk, salon, lorena, consulta, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina])
    entry = paid(salon, source, ana)

    res = join(desk, salon, source, into, [ana])

    assert res.status_code == 200
    entry.refresh_from_db()
    assert entry.appointment_id == into.pk
    billing = {one['name']: one['billing']['state'] for one in res.data['attendees']}
    assert billing['Ana'] == 'paid'


def test_a_paid_client_cannot_move_to_another_service(desk, salon, lorena, consulta, sesion, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, sesion, FIVE, [carolina])
    entry = paid(salon, source, ana)

    res = join(desk, salon, source, into, [ana])

    assert (res.status_code, res.data['code']) == (409, 'join_paid_other_service')
    entry.refresh_from_db()
    assert entry.appointment_id == source.pk
    assert roster(source) == {'Ana'}


def test_a_voided_payment_stays_behind(desk, salon, lorena, consulta, sesion, people):
    """A void is history of this turn, and it charges nothing: no reason to refuse."""
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, sesion, FIVE, [carolina])
    entry = paid(salon, source, ana)
    CashEntry.objects.filter(pk=entry.pk).update(voided_at=timezone.now(), voided_on=THREE.date())

    res = join(desk, salon, source, into, [ana])

    assert res.status_code == 200
    entry.refresh_from_db()
    assert entry.appointment_id == source.pk


def test_a_full_turn_takes_nobody_and_nothing_moves(desk, salon, lorena, consulta, people):
    ana, belen, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana, belen])
    into = turn(salon, lorena, consulta, FIVE, [carolina], capacity=2)

    res = join(desk, salon, source, into, [ana, belen])

    assert (res.status_code, res.data['code']) == (409, 'join_full')
    assert roster(source) == {'Ana', 'Belén'}
    assert roster(into) == {'Carolina'}


def test_a_client_already_there_is_refused(desk, salon, lorena, consulta, people):
    ana, belen, _ = people
    source = turn(salon, lorena, consulta, THREE, [ana, belen])
    into = turn(salon, lorena, consulta, FIVE, [ana])

    res = join(desk, salon, source, into, [ana])

    assert (res.status_code, res.data['code']) == (409, 'join_already_there')


def test_a_client_outside_the_turn_is_refused(desk, salon, lorena, consulta, people):
    ana, belen, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina])

    res = join(desk, salon, source, into, [belen])

    assert (res.status_code, res.data['code']) == (400, 'join_not_in_turn')


def test_two_professionals_cannot_be_joined(desk, salon, lorena, consulta, people, django_user_model):
    ana, _, carolina = people
    diego = Membership.objects.create(
        user=django_user_model.objects.create_user(email='diego@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, diego, consulta, FIVE, [carolina])

    res = join(desk, salon, source, into, [ana])

    assert (res.status_code, res.data['code']) == (400, 'join_other_professional')


def test_a_turn_cannot_join_itself(desk, salon, lorena, consulta, people):
    source = turn(salon, lorena, consulta, THREE, people[:1])

    res = join(desk, salon, source, source, people[:1])

    assert (res.status_code, res.data['code']) == (400, 'join_same')


def test_a_request_still_waiting_cannot_take_clients(desk, salon, lorena, consulta, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina], status=Appointment.Status.PENDING)

    res = join(desk, salon, source, into, [ana])

    assert (res.status_code, res.data['code']) == (409, 'join_not_scheduled')


def test_a_turn_with_attendance_marked_is_left_alone(desk, salon, lorena, consulta, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina])
    into.client_links.update(attendance=AppointmentClient.Attendance.ATTENDED)

    res = join(desk, salon, source, into, [ana])

    assert (res.status_code, res.data['code']) == (409, 'attendance_marked')
    assert roster(source) == {'Ana'}


def test_the_professional_hears_of_it_once(desk, salon, lorena, consulta, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina])

    join(desk, salon, source, into, [ana], reason='Reagendado')

    notices = Notification.objects.filter(recipient=lorena)
    assert [(n.verb, n.appointment_id) for n in notices] == [(Notification.Verb.APPOINTMENT_UPDATED, into.pk)]


def test_staff_may_join_within_their_own_day(salon, lorena, consulta, people):
    ana, _, carolina = people
    source = turn(salon, lorena, consulta, THREE, [ana])
    into = turn(salon, lorena, consulta, FIVE, [carolina])

    res = join(lorena.user, salon, source, into, [ana])

    assert res.status_code == 200
    assert not Notification.objects.exists()  # nobody is told what they did themselves


def test_staff_may_not_join_a_colleagues_turns(salon, lorena, consulta, people, django_user_model):
    ana, _, carolina = people
    diego = Membership.objects.create(
        user=django_user_model.objects.create_user(email='diego@example.com', password='pw'),
        tenant=salon, attends_appointments=True,
    )
    source = turn(salon, diego, consulta, THREE, [ana])
    into = turn(salon, diego, consulta, FIVE, [carolina])

    res = join(lorena.user, salon, source, into, [ana])

    assert res.status_code == 403
    assert roster(source) == {'Ana'}
