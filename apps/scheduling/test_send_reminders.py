"""
The reminder sweep. Every test here drives the real command against the real
database and fakes exactly one thing: the HTTP call to the push service.

What is being proven is the SELECTION -- which appointments come due, whose
devices hear about it, and what stops a second reminder -- because that is where
the behaviour lives. pywebpush's own encryption is its business.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pywebpush import WebPushException

from apps.accounts.models import Membership, PushSubscription
from apps.scheduling.models import Appointment, Category, Client, Service
from apps.tenancy.models import Tenant


@pytest.fixture(autouse=True)
def vapid(settings):
    """Every test but one runs with push configured; the command refuses to start
    otherwise, which is itself a test below."""
    settings.VAPID_PRIVATE_KEY = 'test-private-key'
    settings.VAPID_SUBJECT = 'mailto:test@example.com'


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', timezone='America/Argentina/Buenos_Aires')


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    Membership.objects.create(
        user=user, tenant=salon, role=Membership.Role.STAFF, attends_appointments=True
    )
    return user


@pytest.fixture
def subscription(stylist):
    return PushSubscription.objects.create(
        user=stylist,
        endpoint='https://push.example.com/abc',
        p256dh='key-material',
        auth='auth-secret',
    )


@pytest.fixture
def service(salon):
    category = Category.objects.create(tenant=salon, name='Hair')
    return Service.objects.create(
        tenant=salon, category=category, name='Corte', duration=timedelta(minutes=30)
    )


@pytest.fixture
def booking(salon, stylist, service):
    """Books an appointment `minutes` from now for the stylist."""

    def _book(minutes):
        start = timezone.now() + timedelta(minutes=minutes)
        appointment = Appointment.objects.create(
            tenant=salon,
            professional=stylist.memberships.get(),
            service=service,
            start=start,
            end=start + service.duration,
        )
        client = Client.objects.create(tenant=salon, name='Ana')
        appointment.clients.add(client)
        return appointment

    return _book


@pytest.fixture
def sent():
    """Stands in for the push service. Yields the mock so a test can read what
    was delivered, or make the next delivery fail."""
    with patch('apps.scheduling.management.commands.send_reminders.webpush') as mock:
        yield mock


def test_refuses_to_run_without_vapid_keys(db, settings):
    settings.VAPID_PRIVATE_KEY = ''

    with pytest.raises(CommandError, match='not configured'):
        call_command('send_reminders')


def test_reminds_an_appointment_inside_the_lead_window(booking, subscription, sent):
    appointment = booking(minutes=20)  # default lead is 30

    call_command('send_reminders')

    assert sent.call_count == 1
    appointment.refresh_from_db()
    assert appointment.reminder_sent_at is not None


def test_stays_quiet_until_the_lead_window_opens(booking, subscription, sent):
    appointment = booking(minutes=90)

    call_command('send_reminders')

    assert sent.call_count == 0
    appointment.refresh_from_db()
    assert appointment.reminder_sent_at is None


def test_honours_a_longer_lead_time_on_the_user(booking, subscription, sent, stylist):
    stylist.reminder_lead = timedelta(hours=2)
    stylist.save(update_fields=['reminder_lead'])
    booking(minutes=90)  # outside the default 30, inside this user's 2 hours

    call_command('send_reminders')

    assert sent.call_count == 1


def test_never_reminds_twice(booking, subscription, sent):
    booking(minutes=20)

    call_command('send_reminders')
    call_command('send_reminders')

    assert sent.call_count == 1


def test_a_late_sweep_still_reminds_an_appointment_that_has_not_started(
    booking, subscription, sent
):
    """The self-healing property: the cron was down when this came due, and the
    turn has not happened yet, so it is still worth saying."""
    booking(minutes=2)

    call_command('send_reminders')

    assert sent.call_count == 1


def test_ignores_an_appointment_that_already_started(booking, subscription, sent):
    booking(minutes=-5)

    call_command('send_reminders')

    assert sent.call_count == 0


def test_ignores_a_cancelled_appointment(booking, subscription, sent):
    appointment = booking(minutes=20)
    appointment.cancel(reason='client called off')

    call_command('send_reminders')

    assert sent.call_count == 0


def test_sends_to_every_device_the_professional_registered(booking, stylist, subscription, sent):
    PushSubscription.objects.create(
        user=stylist,
        endpoint='https://push.example.com/laptop',
        p256dh='other-material',
        auth='other-secret',
    )
    booking(minutes=20)

    call_command('send_reminders')

    assert sent.call_count == 2


def test_leaves_the_appointment_unmarked_when_nobody_is_subscribed(booking, sent):
    """Not marked as sent: allowing notifications five minutes from now should
    still get this person their reminder."""
    appointment = booking(minutes=20)

    call_command('send_reminders')

    assert sent.call_count == 0
    appointment.refresh_from_db()
    assert appointment.reminder_sent_at is None


def test_does_not_remind_a_professional_about_someone_elses_appointment(
    booking, subscription, salon, service, django_user_model, sent
):
    other = django_user_model.objects.create_user(email='o@example.com', password='pw')
    membership = Membership.objects.create(
        user=other, tenant=salon, role=Membership.Role.STAFF, attends_appointments=True
    )
    start = timezone.now() + timedelta(minutes=20)
    Appointment.objects.create(
        tenant=salon,
        professional=membership,
        service=service,
        start=start,
        end=start + service.duration,
    )

    call_command('send_reminders')

    # The subscribed stylist has no appointment of their own, and the one that is
    # due belongs to a colleague with no device registered.
    assert sent.call_count == 0


def test_the_payload_reads_in_the_tenants_timezone(booking, subscription, sent, salon):
    """A phone in another zone must still show the hour written in the agenda."""
    appointment = booking(minutes=20)
    local = appointment.start.astimezone(
        __import__('zoneinfo').ZoneInfo(salon.timezone)
    )

    call_command('send_reminders')

    payload = sent.call_args.kwargs['data']
    assert f'{local:%H:%M}' in payload
    assert 'Corte' in payload
    assert 'Ana' in payload


@pytest.mark.parametrize('status_code', [404, 410])
def test_prunes_a_subscription_the_push_service_reports_gone(
    booking, subscription, sent, status_code
):
    """RFC 8030: a push service answers 404 once a subscription has expired.
    Deleting the row IS the unsubscribe path -- there is nothing else to clean."""
    response = type('Response', (), {'status_code': status_code})()
    sent.side_effect = WebPushException('gone', response=response)
    appointment = booking(minutes=20)

    call_command('send_reminders')

    assert not PushSubscription.objects.filter(pk=subscription.pk).exists()
    appointment.refresh_from_db()
    # Nothing was delivered, so nothing was said: leave it for the next sweep.
    assert appointment.reminder_sent_at is None


def test_keeps_the_subscription_when_the_push_service_merely_errors(
    booking, subscription, sent
):
    """A 500 is the push service having a bad day, not the browser going away.
    Deleting on it would silence a device that is perfectly alive."""
    response = type('Response', (), {'status_code': 500})()
    sent.side_effect = WebPushException('server error', response=response)
    booking(minutes=20)

    call_command('send_reminders')

    assert PushSubscription.objects.filter(pk=subscription.pk).exists()
