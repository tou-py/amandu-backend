"""
The public surface, tested as an outsider: no token, no tenant header, nothing
but a URL.

Half of these are about what does NOT come back. That is the point of the
module they cover -- it is the only place in the product where the caller has
proved nothing about themselves.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, Client, Service, TimeOff, WorkSchedule
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
MONDAY = date(2026, 8, 10)


def local(hour, minute=0, day=MONDAY):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


@pytest.fixture
def anon():
    """No credentials and no tenant header. That is the whole fixture."""
    return APIClient()


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
        class _Frozen:
            @staticmethod
            def now():
                return moment
        monkeypatch.setattr('django.utils.timezone.now', _Frozen.now)
    return freeze


def detail_url(shop):
    return reverse('public:shop-detail', args=[shop.slug])


def availability_url(shop):
    return reverse('public:availability', args=[shop.slug])


def book_url(shop):
    return reverse('public:book', args=[shop.slug])


def payload(stylist, haircut, start, **overrides):
    body = {
        'service': haircut.pk,
        'professional': stylist.pk,
        'start': start.isoformat(),
        'name': 'Ada',
        'phone': '+595981123456',
    }
    body.update(overrides)
    return body


# --- what is visible at all -------------------------------------------------

def test_a_tenant_that_never_opted_in_is_not_there(anon, private_studio):
    """
    The studio in production is an internal diary. A feature that exposed its
    calendar by being deployed would be a breach, not a release.
    """
    assert anon.get(detail_url(private_studio)).status_code == 404


def test_a_suspended_tenant_is_a_404_not_a_403(anon, salon):
    """403 would confirm the shop exists and is behind on its subscription."""
    salon.status = Tenant.Status.SUSPENDED
    salon.save(update_fields=['status'])

    assert anon.get(detail_url(salon)).status_code == 404


def test_the_page_gets_what_it_needs_and_no_more(anon, salon, stylist, haircut):
    body = anon.get(detail_url(salon)).json()

    assert body['name'] == 'Salon'
    assert body['services'] == [
        {'id': haircut.pk, 'name': 'Haircut', 'duration_minutes': 30},
    ]
    assert body['professionals'] == [{'id': stylist.pk, 'name': 'Ana'}]


def test_a_professional_who_does_not_attend_is_not_offered(anon, salon,
                                                           django_user_model):
    """The receptionist books the diary and is never booked into it."""
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)

    assert anon.get(detail_url(salon)).json()['professionals'] == []


# --- availability -----------------------------------------------------------

def test_availability_returns_times_and_never_names(anon, salon, stylist, haircut,
                                                    open_monday, _monday_morning):
    booked = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30),
    )
    booked.client_links.create(client=Client.objects.create(tenant=salon, name='Grace'))

    body = anon.get(availability_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'from': MONDAY.isoformat(), 'to': MONDAY.isoformat(),
    }).json()

    assert body[MONDAY.isoformat()] == [
        local(9, 30).isoformat(), local(10).isoformat(), local(10, 30).isoformat(),
    ]
    assert 'Grace' not in str(body)


def test_a_service_from_another_shop_is_not_found(anon, salon, stylist, open_monday,
                                                  private_studio):
    theirs = Service.objects.create(
        tenant=private_studio, name='Reformer', duration=timedelta(minutes=50),
    )

    response = anon.get(availability_url(salon), {
        'service': theirs.pk, 'professional': stylist.pk,
    })

    assert response.status_code == 404


def test_the_horizon_is_clamped_not_rejected(anon, salon, stylist, haircut,
                                             open_monday, _monday_morning):
    """Asking for two years back gets sixty days, because a page asking for too
    much wants as much as it can have."""
    body = anon.get(availability_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'from': MONDAY.isoformat(), 'to': (MONDAY + timedelta(days=730)).isoformat(),
    }).json()

    assert len(body) == 61


# --- booking ----------------------------------------------------------------

def test_a_stranger_can_ask_for_a_slot(anon, salon, stylist, haircut, open_monday,
                                       _monday_morning):
    response = anon.post(book_url(salon), payload(stylist, haircut, local(9)), format='json')

    assert response.status_code == 201
    appointment = Appointment.objects.get()
    assert appointment.status == Appointment.Status.PENDING
    assert appointment.source == Appointment.Source.PUBLIC
    assert appointment.created_by is None
    assert appointment.end == local(9, 30)


def test_the_shop_is_told_a_request_arrived(anon, salon, stylist, haircut,
                                            open_monday, _monday_morning):
    """
    Without this the shop only learns about a request if somebody happens to
    have the agenda open, and the slot is held the whole time it waits.
    """
    anon.post(book_url(salon), payload(stylist, haircut, local(9)), format='json')

    note = Notification.objects.get()
    assert note.recipient == stylist
    assert note.verb == Notification.Verb.APPOINTMENT_REQUESTED
    # The one verb with no actor: nobody inside the tenant did this.
    assert note.actor is None


def test_a_refused_request_notifies_nobody(anon, salon, stylist, haircut, open_monday,
                                           _monday_morning):
    """The notification is written inside the same transaction as the booking,
    so a rejected slot cannot leave a ghost in the bell."""
    anon.post(book_url(salon), payload(stylist, haircut, local(3)), format='json')

    assert not Notification.objects.exists()


def test_the_reply_hands_back_no_handle_on_the_booking(anon, salon, stylist, haircut,
                                                       open_monday, _monday_morning):
    """
    No appointment id, no client id. Nothing here authenticates whoever would
    use them, so anything returned is a handle on a stranger's booking for
    whoever guesses it next.
    """
    body = anon.post(
        book_url(salon), payload(stylist, haircut, local(9)), format='json',
    ).json()
    appointment = Appointment.objects.get()

    assert str(appointment.pk) not in str(body)
    assert set(body) == {'detail'}


def test_a_slot_outside_opening_hours_is_refused(anon, salon, stylist, haircut,
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
    response = anon.post(
        book_url(salon), payload(stylist, haircut, local(15)), format='json',
    )

    assert response.status_code == 400
    assert not Appointment.objects.exists()


def test_a_slot_on_a_closed_day_is_refused(anon, salon, stylist, haircut, open_monday,
                                           _monday_morning):
    TimeOff.objects.create(
        tenant=salon, professional=None,
        start=local(0), end=local(0, day=MONDAY + timedelta(days=1)), reason='Holiday',
    )

    response = anon.post(
        book_url(salon), payload(stylist, haircut, local(9)), format='json',
    )

    assert response.status_code == 400


def test_a_taken_slot_is_refused(anon, salon, stylist, haircut, open_monday,
                                 _monday_morning):
    Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30),
    )

    response = anon.post(
        book_url(salon), payload(stylist, haircut, local(9)), format='json',
    )

    assert response.status_code == 400


def test_a_professional_from_another_shop_is_refused(anon, salon, haircut, open_monday,
                                                     private_studio, django_user_model,
                                                     _monday_morning):
    """A guessed id from another tenant must resolve to nothing, not to
    somebody else's staff."""
    user = django_user_model.objects.create_user(email='x@example.com', password='pw')
    theirs = Membership.objects.create(
        user=user, tenant=private_studio, attends_appointments=True,
    )

    response = anon.post(
        book_url(salon), payload(theirs, haircut, local(9)), format='json',
    )

    assert response.status_code == 400
    assert not Appointment.objects.exists()


def test_a_second_booking_from_one_phone_is_the_same_person(anon, salon, stylist,
                                                            haircut, open_monday,
                                                            _monday_morning):
    """The phone is the client's identity inside a tenant, so a regular booking
    again must not split their history in two."""
    anon.post(book_url(salon), payload(stylist, haircut, local(9)), format='json')
    anon.post(book_url(salon), payload(stylist, haircut, local(10)), format='json')

    assert Client.objects.count() == 1
    assert Appointment.objects.count() == 2


def test_a_regular_cannot_be_renamed_by_a_stranger(anon, salon, stylist, haircut,
                                                   open_monday, _monday_morning):
    """
    Guessing a known phone number must not let anyone rewrite that person's
    name in the shop's own files. Nothing here proved they own the number.
    """
    Client.objects.create(tenant=salon, name='Grace Hopper', phone='+595981123456')

    anon.post(
        book_url(salon),
        payload(stylist, haircut, local(9), name='Not Grace'),
        format='json',
    )

    assert Client.objects.get().name == 'Grace Hopper'


def test_a_time_in_the_past_is_refused(anon, salon, stylist, haircut, open_monday,
                                       freeze_to):
    freeze_to(local(10, 15))

    response = anon.post(
        book_url(salon), payload(stylist, haircut, local(9)), format='json',
    )

    assert response.status_code == 400


def test_booking_into_a_shop_that_never_opted_in_is_a_404(anon, private_studio,
                                                          haircut, stylist,
                                                          _monday_morning):
    response = anon.post(
        book_url(private_studio), payload(stylist, haircut, local(9)), format='json',
    )

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
