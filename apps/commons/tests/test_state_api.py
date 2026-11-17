from datetime import timedelta

import pytest
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Membership, Notification
from apps.commons.tests.helpers import api
from apps.accounting.models import CashEntry
from apps.scheduling.models import AgendaVersion, Appointment, AppointmentClient, Category, Client, Plan, Service, Subscription

STATE_URL = reverse('state')
TOMORROW = timezone.now().replace(microsecond=0) + timedelta(days=1)


@pytest.fixture
def receptionist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


def book(salon, stylist, haircut, minutes=0):
    appointment = Appointment.objects.create(
        tenant=salon,
        professional=stylist,
        service=haircut,
        start=TOMORROW + timedelta(minutes=minutes),
        end=TOMORROW + timedelta(minutes=minutes + 30),
    )
    appointment.clients.add(Client.objects.create(tenant=salon, name='Ada'))
    return appointment


def token(user, tenant, key='appointments'):
    res = api(user, tenant).get(STATE_URL)
    assert res.status_code == 200
    return res.data[key]


def test_a_quiet_diary_answers_the_same_token_twice(receptionist, salon, stylist, haircut):
    book(salon, stylist, haircut)

    assert token(receptionist, salon) == token(receptionist, salon)


# transaction=True: the counter moves at commit (AgendaVersion), and a plain
# `db` test never commits.
@pytest.mark.django_db(transaction=True)
def test_every_kind_of_change_the_agenda_shows_moves_the_token(receptionist, salon, stylist, haircut):
    """
    One write per table the agenda reads. Before the counter, only the first
    four (a turn's own row) moved the token: a payment, a mark of attendance or
    a plan changed on one device stayed invisible on every other one.
    """
    plan = Plan.objects.create(tenant=salon, name='Libre', price=100)
    category = Category.objects.create(tenant=salon, name='Pelo')
    appointment = book(salon, stylist, haircut)
    link = appointment.client_links.get()
    changes = {
        'a turn edited': lambda: Appointment.objects.filter(pk=appointment.pk).update(notes='toalla'),
        'attendance marked': lambda: AppointmentClient.objects.filter(pk=link.pk).update(attendance='attended'),
        'a payment': lambda: CashEntry.objects.create(
            tenant=salon, kind='income', amount=100, occurred_on=timezone.localdate(), concept='Corte',
            appointment=appointment, client=link.client,
        ),
        'a subscription': lambda: Subscription.objects.create(
            tenant=salon, client=link.client, plan=plan, start_date=timezone.localdate(),
        ),
        'a plan repriced': lambda: Plan.objects.filter(pk=plan.pk).update(price=200),
        "a plan's categories": lambda: plan.categories.add(category),
        'a client renamed': lambda: Client.objects.filter(pk=link.client_id).update(name='Ada L'),
        'a service repriced': lambda: Service.objects.filter(pk=haircut.pk).update(price=900),
        'the team changed': lambda: Membership.objects.filter(pk=stylist.pk).update(attends_appointments=False),
        'a turn deleted': lambda: Appointment.objects.filter(pk=appointment.pk).delete(),
    }
    for change, write in changes.items():
        before = token(receptionist, salon)
        write()
        assert token(receptionist, salon) != before, change


@pytest.mark.django_db(transaction=True)
def test_another_tenants_diary_does_not_move_this_one(
    receptionist, salon, clinic, stylist, haircut, django_user_model
):
    """The whole point of a shared probe: a busy neighbour must not make every
    tab in this tenant refetch four endpoints for nothing."""
    other_user = django_user_model.objects.create_user(email='o@example.com', password='pw')
    other = Membership.objects.create(
        user=other_user, tenant=clinic, attends_appointments=True,
    )
    other_service = Service.objects.create(
        tenant=clinic, name='Consulta', duration=timedelta(minutes=30),
    )
    before = token(receptionist, salon)

    book(clinic, other, other_service)

    assert token(receptionist, salon) == before


@pytest.mark.django_db(transaction=True)
def test_a_business_can_still_be_deleted_whole(salon, stylist, haircut):
    """Turns protect what they point at, so offboarding clears them first; the
    business then cascades to its team, clients and services. Each fires its
    trigger at commit, after the business is gone, and the counter must not try
    to bring it back (a foreign key violation that would block the delete)."""
    book(salon, stylist, haircut)

    with transaction.atomic():
        Appointment.objects.filter(tenant=salon).delete()
        salon.delete()

    assert not AgendaVersion.objects.exists()


def test_the_feed_token_moves_when_a_row_is_read(receptionist, salon, stylist, haircut):
    """Marking everything read changes no row count and no id, which is why the
    unread count is the third number in this token."""
    recipient = Membership.objects.get(user=receptionist, tenant=salon)
    notification = Notification.objects.create(
        recipient=recipient,
        appointment=book(salon, stylist, haircut),
        verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    unread = token(receptionist, salon, 'notifications')

    notification.read_at = timezone.now()
    notification.save(update_fields=['read_at'])

    assert token(receptionist, salon, 'notifications') != unread


def test_a_feed_belongs_to_one_membership(receptionist, salon, stylist, haircut):
    """Two people in the same tenant share the diary token and never the feed
    one: a notification addressed to the stylist is not news for the desk."""
    before = token(receptionist, salon, 'notifications')

    Notification.objects.create(
        recipient=stylist,
        appointment=book(salon, stylist, haircut),
        verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )

    assert token(receptionist, salon, 'notifications') == before


def test_the_probe_does_not_scale_queries(receptionist, salon, stylist, haircut):
    """
    The reason this endpoint exists is that it stays cheap while the diary grows.

    Two things are pinned, because the query count alone would not catch the
    obvious wrong implementation: somebody answering this by serialising the
    rows would still run one query for them. So the body is measured too -- a
    probe is a handful of bytes whatever the diary holds, and the moment it
    starts carrying data it has become the fifth request it was written to
    remove.
    """
    http = api(receptionist, salon)
    book(salon, stylist, haircut)
    with CaptureQueriesContext(connection) as small:
        one_row = http.get(STATE_URL)

    for minute in range(1, 25):
        book(salon, stylist, haircut, minutes=minute * 45)
    with CaptureQueriesContext(connection) as large:
        grown = http.get(STATE_URL)

    assert len(large) == len(small)
    # Four: the user, the membership, the diary counter and the feed aggregate.
    assert len(small) <= 4, [q['sql'] for q in small.captured_queries]
    # 25 appointments later, the answer is still two tokens. The tokens
    # themselves grow by the digits of a counter, which is what the slack allows.
    assert len(grown.content) < 200
    assert len(grown.content) - len(one_row.content) <= 2
