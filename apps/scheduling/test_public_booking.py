"""
The rules behind a stranger's booking, driven through the public page: no
token, no tenant header, nothing but a URL and a form.

test_public_page.py asserts on what the page draws. This file is about what a
POST is allowed to do -- the one place in the product where the caller has
proved nothing about themselves.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, Client, Service, TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
MONDAY = date(2026, 8, 10)


def local(hour, minute=0, day=MONDAY):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY',
        timezone='America/Asuncion', public_booking=True,
    )


@pytest.fixture
def private_studio(db):
    """The tenant already in production: an internal diary that never opted in."""
    return Tenant.objects.create(
        name='Pilates', slug='pilates', country='PY', timezone='America/Asuncion',
    )


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(
        email='s@example.com', password='pw', first_name='Ana',
    )
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


@pytest.fixture
def open_monday(db, salon, stylist):
    return WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(11),
    )


@pytest.fixture
def _monday_morning(freeze_to):
    """Every test runs at 08:00 on that Monday, so the slots are all ahead."""
    freeze_to(local(8))


@pytest.fixture
def freeze_to(monkeypatch):
    def freeze(moment):
        monkeypatch.setattr('django.utils.timezone.now', lambda: moment)
    return freeze


def page_url(shop):
    return reverse('booking-page', args=[shop.slug])


def book(client, shop, stylist, haircut, start, **overrides):
    body = {
        'service': haircut.pk,
        'professional': stylist.pk,
        'start': start.isoformat(),
        'name': 'Ada',
        'phone': '+595981123456',
    }
    body.update(overrides)
    return client.post(page_url(shop), body)


# --- what is visible at all -------------------------------------------------

def test_a_suspended_tenant_is_a_404_not_a_403(client, salon):
    """403 would confirm the shop exists and is behind on its subscription."""
    salon.status = Tenant.Status.SUSPENDED
    salon.save(update_fields=['status'])

    assert client.get(page_url(salon)).status_code == 404


def test_a_professional_who_does_not_attend_is_not_offered(client, salon, stylist, haircut,
                                                           django_user_model):
    """The receptionist books the diary and is never booked into it."""
    user = django_user_model.objects.create_user(
        email='r@example.com', password='pw', first_name='Rita',
    )
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)

    assert 'Rita' not in client.get(page_url(salon)).content.decode()


# --- booking ----------------------------------------------------------------

def test_a_stranger_can_ask_for_a_slot(client, salon, stylist, haircut, open_monday,
                                       _monday_morning):
    book(client, salon, stylist, haircut, local(9))

    appointment = Appointment.objects.get()
    assert appointment.status == Appointment.Status.PENDING
    assert appointment.source == Appointment.Source.PUBLIC
    assert appointment.created_by is None
    assert appointment.end == local(9, 30)


def test_the_shop_is_told_a_request_arrived(client, salon, stylist, haircut,
                                            open_monday, _monday_morning):
    """
    Without this the shop only learns about a request if somebody happens to
    have the agenda open, and the slot is held the whole time it waits.
    """
    book(client, salon, stylist, haircut, local(9))

    note = Notification.objects.get()
    assert note.recipient == stylist
    assert note.verb == Notification.Verb.APPOINTMENT_REQUESTED
    # The one verb with no actor: nobody inside the tenant did this.
    assert note.actor is None


def test_a_refused_request_notifies_nobody(client, salon, stylist, haircut, open_monday,
                                           _monday_morning):
    """The notification is written inside the same transaction as the booking,
    so a rejected slot cannot leave a ghost in the bell."""
    book(client, salon, stylist, haircut, local(3))

    assert not Notification.objects.exists()


def test_a_slot_outside_opening_hours_is_refused(client, salon, stylist, haircut,
                                                 open_monday, _monday_morning):
    """
    Hand-edited times are the reason availability is re-derived on the write.
    The overlap constraint would have let this one through: 15:00 clashes with
    nothing, it is simply a time the shop is shut.

    Three in the afternoon and not three in the morning on purpose. The shop
    opens 09:00-11:00 and the clock here reads 08:00, so an early hour would be
    refused for being in the past and this test would pass without the opening
    hours ever being consulted.
    """
    book(client, salon, stylist, haircut, local(15))

    assert not Appointment.objects.exists()


def test_a_slot_on_a_closed_day_is_refused(client, salon, stylist, haircut, open_monday,
                                           _monday_morning):
    TimeOff.objects.create(
        tenant=salon, professional=None,
        start=local(0), end=local(0, day=MONDAY + timedelta(days=1)), reason='Holiday',
    )

    book(client, salon, stylist, haircut, local(9))

    assert not Appointment.objects.exists()


def test_a_taken_slot_is_refused(client, salon, stylist, haircut, open_monday,
                                 _monday_morning):
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30),
    )

    book(client, salon, stylist, haircut, local(9))

    assert Appointment.objects.count() == 1


def test_a_service_from_another_shop_is_refused(client, salon, stylist, open_monday,
                                                private_studio, haircut, _monday_morning):
    theirs = Service.objects.create(
        tenant=private_studio, name='Reformer', duration=timedelta(minutes=50),
    )

    book(client, salon, stylist, theirs, local(9))

    assert not Appointment.objects.exists()


def test_a_professional_from_another_shop_is_refused(client, salon, haircut, open_monday,
                                                     private_studio, django_user_model,
                                                     _monday_morning):
    """A guessed id from another tenant must resolve to nothing, not to
    somebody else's staff."""
    user = django_user_model.objects.create_user(email='x@example.com', password='pw')
    theirs = Membership.objects.create(
        user=user, tenant=private_studio, attends_appointments=True,
    )

    book(client, salon, theirs, haircut, local(9))

    assert not Appointment.objects.exists()


def test_a_second_booking_from_one_phone_is_the_same_person(client, salon, stylist,
                                                            haircut, open_monday,
                                                            _monday_morning):
    """The phone is the client's identity inside a tenant, so a regular booking
    again must not split their history in two."""
    book(client, salon, stylist, haircut, local(9))
    book(client, salon, stylist, haircut, local(10))

    assert Client.objects.count() == 1
    assert Appointment.objects.count() == 2


def test_a_regular_cannot_be_renamed_by_a_stranger(client, salon, stylist, haircut,
                                                   open_monday, _monday_morning):
    """
    Guessing a known phone number must not let anyone rewrite that person's
    name in the shop's own files. Nothing here proved they own the number.
    """
    Client.objects.create(tenant=salon, name='Grace Hopper', phone='+595981123456')

    book(client, salon, stylist, haircut, local(9), name='Not Grace')

    assert Client.objects.get().name == 'Grace Hopper'


def test_a_time_in_the_past_is_refused(client, salon, stylist, haircut, open_monday,
                                       freeze_to):
    freeze_to(local(10, 15))

    book(client, salon, stylist, haircut, local(9))

    assert not Appointment.objects.exists()


def test_booking_into_a_shop_that_never_opted_in_is_a_404(client, private_studio,
                                                          haircut, stylist,
                                                          _monday_morning):
    response = book(client, private_studio, stylist, haircut, local(9))

    assert response.status_code == 404


# --- what the shop does with a request --------------------------------------

def test_a_pending_request_holds_the_slot_against_the_diary(salon, stylist, haircut):
    """
    The overlap constraint excludes only 'cancelled', so a request waiting for
    an answer cannot be quietly booked over from inside the shop.
    """
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30), status=Appointment.Status.PENDING,
    )

    with pytest.raises(Exception):
        Appointment.objects.create(
            tenant=salon, professional=stylist, service=haircut,
            start=local(9), end=local(9, 30),
        )


def test_confirming_turns_a_request_into_a_booking(salon, stylist, haircut):
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30), status=Appointment.Status.PENDING,
    )

    appointment.confirm()

    assert appointment.status == Appointment.Status.SCHEDULED


def test_turning_a_request_down_is_just_cancelling_it(salon, stylist, haircut):
    """The shop saying no and a booking being called off give the slot back the
    same way, so they are the same transition."""
    appointment = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30), status=Appointment.Status.PENDING,
    )

    appointment.cancel(reason='Fully booked')

    assert appointment.status == Appointment.Status.CANCELLED
