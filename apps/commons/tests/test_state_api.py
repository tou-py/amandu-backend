from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, Client, Service
from apps.tenancy.models import Tenant

STATE_URL = reverse('state')
TOMORROW = timezone.now().replace(microsecond=0) + timedelta(days=1)


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
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


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


def test_every_kind_of_change_moves_the_token(receptionist, salon, stylist, haircut):
    """
    Created, edited, cancelled, deleted. The deletion is the one a bare
    max(updated_at) cannot see, and the reason the count travels beside it.
    """
    before = token(receptionist, salon)

    appointment = book(salon, stylist, haircut)
    created = token(receptionist, salon)
    assert created != before

    appointment.notes = 'trae su propia toalla'
    appointment.save(update_fields=['notes', 'updated_at'])
    edited = token(receptionist, salon)
    assert edited != created

    appointment.cancel(reason='se enfermó')
    cancelled = token(receptionist, salon)
    assert cancelled != edited

    appointment.delete()
    assert token(receptionist, salon) != cancelled


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
    # Four: the user, the membership, the diary aggregate and the feed aggregate.
    assert len(small) <= 4, [q['sql'] for q in small.captured_queries]
    # 25 appointments later, the answer is still two tokens. The tokens
    # themselves grow by the digits of a count, which is what the slack allows.
    assert len(grown.content) < 200
    assert len(grown.content) - len(one_row.content) <= 2
