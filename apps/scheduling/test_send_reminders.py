"""
The reminder sweep. Every test here drives the real command against the real
database and fakes exactly one thing: the HTTP call to the push service.

What is being proven is the SELECTION -- which appointments come due, whose
devices hear about it, and what stops a second reminder -- because that is where
the behaviour lives. pywebpush's own encryption is its business.
"""
import json
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from pywebpush import WebPushException

from apps.accounts.models import Membership, Notification, PushSubscription
from apps.scheduling.models import Appointment, Category, Client, Plan, Service, Subscription
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


# --------------------------------------------------------------- notifications
#
# The other half of the same tick. Same shape of proof: selection and
# idempotency, with the HTTP call faked.


@pytest.fixture
def cancellation(salon, stylist, booking):
    """A slot of the stylist's, called off by somebody else on the team."""

    # One manager for the whole fixture: created inside _cancel it would collide
    # on the unique email the second time a test calls it.
    manager = Membership.objects.create(
        user=get_user_model().objects.create_user(email='m@example.com', password='pw'),
        tenant=salon,
        role=Membership.Role.COORDINATOR,
    )

    def _cancel(minutes=600):
        # Far enough out to stay clear of the reminder lead window, so these
        # tests count notification pushes and nothing else. `minutes` exists
        # because two of them must not overlap on the same professional.
        appointment = booking(minutes)
        return Notification.objects.create(
            recipient=stylist.memberships.get(),
            actor=manager,
            appointment=appointment,
            verb=Notification.Verb.APPOINTMENT_CANCELLED,
        )

    return _cancel


def test_pushes_a_notification_nobody_has_been_told_about(cancellation, subscription, sent):
    notification = cancellation()

    call_command('send_reminders')

    assert sent.call_count == 1
    notification.refresh_from_db()
    assert notification.pushed_at is not None


def test_never_pushes_the_same_notification_twice(cancellation, subscription, sent):
    cancellation()

    call_command('send_reminders')
    call_command('send_reminders')

    assert sent.call_count == 1


def test_a_notification_waits_for_a_device_instead_of_being_marked_pushed(cancellation, sent):
    """No subscription yet: allowing them later must still deliver what waited."""
    notification = cancellation()

    call_command('send_reminders')

    assert sent.call_count == 0
    notification.refresh_from_db()
    assert notification.pushed_at is None


def test_the_push_says_who_cancelled_and_which_slot(cancellation, subscription, sent):
    cancellation()

    call_command('send_reminders')

    payload = json.loads(sent.call_args.kwargs['data'])
    assert payload['title'] == 'Turno cancelado'
    assert 'canceló' in payload['body']
    assert 'Corte' in payload['body']


def test_survives_the_appointment_being_deleted_underneath_it(cancellation, subscription, sent):
    """`appointment` is SET_NULL, so the row outlives the booking it describes."""
    notification = cancellation()
    notification.appointment.delete()

    call_command('send_reminders')

    assert sent.call_count == 1
    payload = json.loads(sent.call_args.kwargs['data'])
    assert payload['title'] == 'Turno cancelado'


def test_two_cancellations_do_not_replace_each_other_on_the_device(
    cancellation, subscription, sent,
):
    """A shared tag means 'replace'. Distinct rows need distinct tags."""
    cancellation(minutes=600)
    cancellation(minutes=700)

    call_command('send_reminders')

    tags = {json.loads(call.kwargs['data'])['tag'] for call in sent.call_args_list}
    assert len(tags) == 2


def test_two_reminders_due_at_once_do_not_replace_each_other(
    booking, subscription, sent, stylist,
):
    """Same rule for reminders, which used to share one constant tag."""
    # A wide lead so both fall due on one sweep. They cannot simply be minutes
    # apart: no_overlap_per_professional forbids two 30-minute slots inside the
    # default 30-minute window for the same person.
    stylist.reminder_lead = timedelta(hours=3)
    stylist.save(update_fields=['reminder_lead'])
    booking(minutes=10)
    booking(minutes=70)

    call_command('send_reminders')

    tags = {json.loads(call.kwargs['data'])['tag'] for call in sent.call_args_list}
    assert len(tags) == 2


# --- The billing half of the sweep -----------------------------------------
#
# Same command, different subject: tenants whose paid period ran out lose access.
# What is being proven here is the SELECTION again -- who gets cut off and, just
# as importantly, who never does.


@pytest.fixture
def paid(db):
    def make(days, status=Tenant.Status.ACTIVE, slug=None):
        """A tenant paid through `days` from today; negative means lapsed."""
        return Tenant.objects.create(
            name=f'Tenant {days}',
            slug=slug or f'tenant-{days}-{status}',
            status=status,
            paid_until=timezone.localdate() + timedelta(days=days),
        )
    return make


def test_suspends_a_tenant_whose_paid_period_ended(paid, sent):
    tenant = paid(days=-1)

    call_command('send_reminders')

    tenant.refresh_from_db()
    assert tenant.status == Tenant.Status.SUSPENDED


def test_leaves_a_tenant_paid_through_today_alone(paid, sent):
    """The comparison is against a date, not an instant: paid through the 31st
    means working all of the 31st, not until midnight of the 30th."""
    tenant = paid(days=0)

    call_command('send_reminders')

    tenant.refresh_from_db()
    assert tenant.status == Tenant.Status.ACTIVE


def test_never_suspends_a_tenant_without_an_expiry(salon, sent):
    """NULL paid_until is the default and means 'never expires' -- every tenant
    onboarded before billing existed is one, and none of them may be cut off."""
    assert salon.paid_until is None

    call_command('send_reminders')

    salon.refresh_from_db()
    assert salon.status == Tenant.Status.ACTIVE


def test_does_not_resurrect_a_closed_tenant(paid, sent):
    """CLOSED is terminal: they left. Only ACTIVE tenants are swept, so the
    status must survive untouched rather than be rewritten to SUSPENDED."""
    tenant = paid(days=-30, status=Tenant.Status.CLOSED)

    call_command('send_reminders')

    tenant.refresh_from_db()
    assert tenant.status == Tenant.Status.CLOSED


def test_suspends_even_when_push_is_not_configured(paid, settings):
    """The ordering guarantee: collecting money does not depend on VAPID keys.
    The command still refuses to go on to the push half, and that is the point --
    the suspension already happened before it did."""
    settings.VAPID_PRIVATE_KEY = ''
    tenant = paid(days=-1)

    with pytest.raises(CommandError, match='not configured'):
        call_command('send_reminders')

    tenant.refresh_from_db()
    assert tenant.status == Tenant.Status.SUSPENDED


# --- The plan digest ------------------------------------------------------------
#
# Once a day, from 05:00 in each business's own timezone, owners and admins hear
# who owes. The clock is the subject here, so every test pins it: `clock(...)`
# sets the instant the whole sweep (and the billing rules under it) believes it
# is.

UTC = ZoneInfo('UTC')


@contextmanager
def clock(*args):
    with patch('django.utils.timezone.now', return_value=datetime(*args, tzinfo=UTC)):
        yield


@pytest.fixture
def team(db, django_user_model):
    """Every role in a tenant, so a test can tell who heard the digest."""

    def make(tenant):
        members = {}
        for role in Membership.Role.values:
            user = django_user_model.objects.create_user(email=f'{role}@{tenant.slug}.com', password='pw')
            members[role] = Membership.objects.create(user=user, tenant=tenant, role=role)
        return members

    return make


@pytest.fixture
def owing(db):
    """A client of `tenant` who has owed a plan since August 2026."""

    def make(tenant):
        plan = Plan.objects.create(tenant=tenant, name='Pilates', price=250000)
        client = Client.objects.create(tenant=tenant, name='Ana')
        return Subscription.objects.create(tenant=tenant, client=client, plan=plan, start_date=date(2026, 8, 1))

    return make


def digests():
    return Notification.objects.filter(verb=Notification.Verb.PLAN_DIGEST)


def test_the_digest_reaches_owners_and_admins_only(salon, team, owing, sent):
    members = team(salon)
    owing(salon)

    with clock(2026, 10, 2, 8, 0):  # 05:00 in Buenos Aires
        call_command('send_reminders')

    assert sorted(n.recipient.role for n in digests()) == ['admin', 'owner']
    assert {n.recipient for n in digests()} == {members['owner'], members['admin']}


def test_no_digest_before_five_in_the_morning_local_time(salon, team, owing, sent):
    team(salon)
    owing(salon)

    with clock(2026, 10, 2, 7, 59):  # 04:59 in Buenos Aires, 07:59 on the server
        call_command('send_reminders')

    assert not digests().exists()
    salon.refresh_from_db()
    assert salon.plan_digest_date is None


def test_one_digest_a_day_however_often_the_sweep_runs(salon, team, owing, sent):
    team(salon)
    owing(salon)

    for hour in (8, 9, 23):
        with clock(2026, 10, 2, hour, 0):
            call_command('send_reminders')
    assert digests().count() == 2

    with clock(2026, 10, 3, 8, 0):  # the next local day
        call_command('send_reminders')
    assert digests().count() == 4


def test_a_missed_morning_still_gets_its_digest_late(salon, team, owing, sent):
    """The server was down from Monday to Friday afternoon: Friday's digest
    goes out on the first run back, not never."""
    team(salon)
    owing(salon)
    Tenant.objects.filter(pk=salon.pk).update(plan_digest_date=date(2026, 9, 28))

    with clock(2026, 10, 2, 18, 0):  # 15:00 in Buenos Aires
        call_command('send_reminders')

    assert digests().count() == 2
    salon.refresh_from_db()
    assert salon.plan_digest_date == date(2026, 10, 2)


def test_nothing_owed_claims_the_day_and_sends_nothing(salon, team, sent):
    team(salon)
    plan = Plan.objects.create(tenant=salon, name='Pilates', price=250000)
    client = Client.objects.create(tenant=salon, name='Ana')
    # Starts in the future: nothing owed, nothing due soon.
    Subscription.objects.create(tenant=salon, client=client, plan=plan, start_date=date(2026, 11, 1))

    with clock(2026, 10, 2, 8, 0):
        call_command('send_reminders')

    assert not digests().exists()
    salon.refresh_from_db()
    assert salon.plan_digest_date == date(2026, 10, 2)


def test_each_tenant_wakes_up_in_its_own_timezone(salon, team, owing, sent):
    tokyo = Tenant.objects.create(name='Tokyo', slug='tokyo', timezone='Asia/Tokyo')
    team(salon)
    team(tokyo)
    owing(salon)
    owing(tokyo)

    with clock(2026, 10, 2, 6, 0):  # 03:00 in Buenos Aires, 15:00 in Tokyo
        call_command('send_reminders')

    assert {n.recipient.tenant for n in digests()} == {tokyo}


def test_the_digest_push_says_who_owes_and_opens_por_cobrar(salon, team, owing, sent):
    members = team(salon)
    owing(salon)
    PushSubscription.objects.create(
        user=members['owner'].user, endpoint='https://push.example.com/o', p256dh='k', auth='a',
    )

    with clock(2026, 10, 2, 8, 0):
        call_command('send_reminders')

    payload = json.loads(sent.call_args.kwargs['data'])
    assert payload['url'] == '/por-cobrar'
    # August, September and October started; Oct 1 + 9 days of grace is not
    # past yet, so two periods are overdue.
    assert payload['body'] == '1 cliente debe Gs. 500.000.'


def test_the_digest_lands_in_the_feed_even_without_push(salon, team, owing, settings):
    """Like the billing sweep, the in-app feed does not depend on VAPID keys:
    the bell is the channel for a device that never allowed push."""
    settings.VAPID_PRIVATE_KEY = ''
    team(salon)
    owing(salon)

    with clock(2026, 10, 2, 8, 0), pytest.raises(CommandError):
        call_command('send_reminders')

    assert digests().count() == 2
