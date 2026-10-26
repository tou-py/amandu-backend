"""
Public requests nobody answered, as the sweep (send_reminders) handles them:
put to the shop's managers after a while, turned down once their hour passed.
Driven through the real command; the push service and WAHA are faked.
"""
from datetime import datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, Client, OutboundMessage, Service
from apps.tenancy.models import Tenant

ZONE = ZoneInfo('America/Asuncion')


def local(days, hour, minute=0):
    """A local wall-clock time `days` from today in the tenant's zone."""
    day = timezone.now().astimezone(ZONE).date() + timedelta(days=days)
    return datetime.combine(day, time(hour, minute)).replace(tzinfo=ZONE)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY', timezone='America/Asuncion')


@pytest.fixture
def receptionist(django_user_model, salon):
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def stylist(django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(salon):
    return Service.objects.create(tenant=salon, name='Corte', duration=timedelta(minutes=30))


@pytest.fixture
def ana(salon):
    return Client.objects.create(tenant=salon, name='Ana', phone='+595981111111')


@pytest.fixture
def turno(salon, stylist, haircut, ana):
    def _turno(start, status=Appointment.Status.SCHEDULED, professional=None):
        appointment = Appointment.objects.create(
            tenant=salon, professional=professional or stylist, service=haircut,
            start=start, end=start + haircut.duration, status=status,
        )
        appointment.clients.set([ana])
        return appointment

    return _turno


WAITING = Notification.Verb.APPOINTMENT_REQUEST_WAITING


@pytest.fixture(autouse=True)
def quiet_push(settings):
    settings.VAPID_PRIVATE_KEY = 'test-private-key'
    settings.VAPID_SUBJECT = 'mailto:test@example.com'
    with patch('apps.scheduling.management.commands.send_reminders.webpush'):
        yield


@pytest.fixture
def owner(django_user_model, salon):
    user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)


@pytest.fixture
def request_(turno):
    """A public request asked for `hours_ago`, for a slot `start`."""

    def _request(hours_ago, start=None):
        appointment = turno(start or local(3, 10), status=Appointment.Status.PENDING)
        Appointment.objects.filter(pk=appointment.pk).update(
            source=Appointment.Source.PUBLIC,
            created_at=timezone.now() - timedelta(hours=hours_ago),
        )
        return appointment

    return _request


@pytest.fixture
def daytime():
    """Pins the shop's clock to 11:00 local, inside waking hours."""
    noon = local(0, 11)
    with patch('django.utils.timezone.now', return_value=noon):
        yield noon


def waiting_for(membership):
    return Notification.objects.filter(recipient=membership, verb=WAITING).count()


def test_a_request_waiting_two_hours_is_put_to_the_managers(daytime, owner, receptionist, stylist, request_):
    request_(hours_ago=3)

    call_command('send_reminders')

    assert waiting_for(owner) == 1
    assert waiting_for(receptionist.memberships.get()) == 1
    assert waiting_for(stylist) == 0


def test_a_fresh_request_is_not_escalated(daytime, owner, request_):
    request_(hours_ago=1)

    call_command('send_reminders')

    assert waiting_for(owner) == 0


def test_a_request_is_escalated_once(daytime, owner, request_):
    request_(hours_ago=3)

    call_command('send_reminders')
    call_command('send_reminders')

    assert waiting_for(owner) == 1


def test_nobody_is_woken_at_night(owner, request_):
    request_(hours_ago=3)

    with patch('django.utils.timezone.now', return_value=local(0, 23)):
        call_command('send_reminders')

    assert waiting_for(owner) == 0


def test_a_manager_is_not_told_about_their_own_request(daytime, owner, turno):
    owner.attends_appointments = True
    owner.save()
    appointment = turno(local(3, 10), status=Appointment.Status.PENDING, professional=owner)
    Appointment.objects.filter(pk=appointment.pk).update(created_at=daytime - timedelta(hours=3))

    call_command('send_reminders')

    assert waiting_for(owner) == 0


def test_a_request_whose_hour_passed_expires_and_frees_the_slot(daytime, settings, owner, salon, ana, request_):
    salon.phone = '+595210000000'
    salon.save()
    ana.whatsapp_opt_in = True
    ana.save()
    expired = request_(hours_ago=30, start=daytime - timedelta(hours=1))

    settings.WAHA_URL = 'http://waha'
    with patch('apps.scheduling.whatsapp.send'):
        call_command('send_reminders')

    expired.refresh_from_db()
    assert expired.status == Appointment.Status.CANCELLED
    assert expired.cancellation_reason == 'Vencida sin respuesta'
    assert OutboundMessage.objects.filter(appointment=expired, to=ana.phone).count() == 1
    assert not Notification.objects.filter(appointment=expired).exclude(verb=WAITING).exists()
