"""
A public request and its answers, from the outside: what the page books, who
hears about it, and what is filed for the client.

Driven through the highest seams there are -- the public page's POST, the
confirm/cancel actions, and `send_reminders`.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling import notify
from apps.scheduling.models import (
    Appointment,
    Client,
    OutboundMessage,
    Service,
    WorkSchedule,
)
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
MONDAY = date(2026, 8, 10)
CLIENT_PHONE = '+595981123456'


def local(hour, minute=0, day=MONDAY):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


@pytest.fixture(autouse=True)
def sender(monkeypatch):
    """A WhatsApp sender connected: what gets filed for the client."""
    monkeypatch.setattr(notify, 'WHATSAPP_SENDER', True)


@pytest.fixture
def now(monkeypatch):
    def freeze(moment):
        monkeypatch.setattr('django.utils.timezone.now', lambda: moment)
    freeze(local(8))
    return freeze


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salon', slug='salon', country='PY', timezone='America/Asuncion',
        addon_reservas=True, phone='+595211234567',
    )


def _professional(django_user_model, salon, name, phone=''):
    user = django_user_model.objects.create_user(
        email=f'{name.lower()}@example.com', password='pw', first_name=name, phone=phone,
    )
    membership = Membership.objects.create(user=user, tenant=salon, attends_appointments=True)
    WorkSchedule.objects.create(
        tenant=salon, professional=membership, weekday=0, start_time=time(9), end_time=time(11),
    )
    return membership


@pytest.fixture
def ana(db, django_user_model, salon):
    return _professional(django_user_model, salon, 'Ana', phone='+595982000001')


@pytest.fixture
def bea(db, django_user_model, salon):
    return _professional(django_user_model, salon, 'Bea')


@pytest.fixture
def coordinator(db, django_user_model, salon):
    user = django_user_model.objects.create_user(
        email='c@example.com', password='pw', first_name='Carla',
    )
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.COORDINATOR)
    return user


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Corte', duration=timedelta(minutes=30))


def page(salon):
    return reverse('booking-page', args=[salon.slug])


def book(client, salon, haircut, start, professional=None, **overrides):
    body = {
        'service': haircut.pk,
        'professional': professional.pk if professional else '',
        'start': start.isoformat(),
        'name': 'Ada Lovelace',
        'phone': CLIENT_PHONE,
        'notify_whatsapp': 'true',
    }
    body.update(overrides)
    return client.post(page(salon), body)


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


def act(user, tenant, appointment, name, **body):
    url = reverse(f'scheduling:appointment-{name}', args=[appointment.pk])
    return api(user, tenant).post(url, body, format='json')


def to(phone):
    return list(OutboundMessage.objects.filter(to=phone).values_list('body', flat=True))


# --- "Cualquiera" -------------------------------------------------------------

def test_cualquiera_is_the_default_and_offers_everyones_slots(client, salon, ana, bea,
                                                               haircut, now):
    Appointment.objects.create(
        tenant=salon, professional=ana, service=haircut, start=local(9), end=local(9, 30),
    )

    who = client.get(page(salon), {'paso': 'profesional'}).content.decode()
    hours = client.get(page(salon), {'paso': 'cuando'}).content.decode()

    assert 'Cualquiera' in who
    # Ana is busy at 09:00 but Bea is not: the slot is still on offer.
    assert local(9).isoformat() in hours


def test_cualquiera_gives_the_turno_to_whoever_is_free(client, salon, ana, bea, haircut, now):
    Appointment.objects.create(
        tenant=salon, professional=ana, service=haircut, start=local(9), end=local(9, 30),
    )

    book(client, salon, haircut, local(9))

    assert Appointment.objects.get(status=Appointment.Status.PENDING).professional == bea


def test_cualquiera_spreads_requests_over_the_team(client, salon, ana, bea, haircut, now):
    """Ana is first by id; one turno already that day sends the next to Bea."""
    Appointment.objects.create(
        tenant=salon, professional=ana, service=haircut, start=local(10), end=local(10, 30),
    )

    book(client, salon, haircut, local(9))

    assert Appointment.objects.get(status=Appointment.Status.PENDING).professional == bea


def test_cualquiera_with_nobody_free_is_refused(client, salon, ana, bea, haircut, now):
    for one in (ana, bea):
        Appointment.objects.create(
            tenant=salon, professional=one, service=haircut, start=local(9), end=local(9, 30),
        )

    book(client, salon, haircut, local(9))

    assert not Appointment.objects.filter(status=Appointment.Status.PENDING).exists()


def test_a_named_professional_is_still_honoured(client, salon, ana, bea, haircut, now):
    book(client, salon, haircut, local(9), professional=bea)

    assert Appointment.objects.get().professional == bea


# --- a request ----------------------------------------------------------------

def test_a_request_tells_the_client_by_whatsapp_and_the_professional_by_push(
        client, salon, ana, haircut, now):
    book(client, salon, haircut, local(9), professional=ana)

    [to_client] = to(CLIENT_PHONE)
    assert 'Ada,' in to_client  # first name only
    assert 'Corte' in to_client and 'lunes 10/08 a las 09:00' in to_client
    assert '+595 21 123 4567' in to_client  # the business's own number, to answer
    # Ana saved a phone, but the team is never written to from Kyo's number.
    assert to(ana.user.phone) == []
    note = Notification.objects.get()
    assert (note.recipient, note.verb) == (ana, Notification.Verb.APPOINTMENT_REQUESTED)


def test_no_box_ticked_no_message_at_all(client, salon, ana, haircut, now):
    book(client, salon, haircut, local(9), professional=ana, notify_whatsapp='')

    assert not OutboundMessage.objects.exists()


def test_an_unticked_box_later_does_not_take_consent_back(client, salon, ana, haircut, now):
    book(client, salon, haircut, local(9), professional=ana)
    book(client, salon, haircut, local(10), professional=ana, notify_whatsapp='')

    assert Client.objects.get().whatsapp_opt_in
    assert len(to(CLIENT_PHONE)) == 2


def test_a_business_without_its_own_number_sends_no_whatsapp(client, salon, ana, haircut, now):
    salon.phone = ''
    salon.save(update_fields=['phone'])

    book(client, salon, haircut, local(9), professional=ana)

    assert not OutboundMessage.objects.exists()
    assert Notification.objects.exists()


def test_without_a_sender_nothing_is_queued(client, salon, ana, haircut, now, monkeypatch):
    monkeypatch.setattr(notify, 'WHATSAPP_SENDER', False)

    book(client, salon, haircut, local(9), professional=ana)

    assert not OutboundMessage.objects.exists()


def test_one_phone_cannot_stack_requests(client, salon, ana, haircut, now):
    for hour in (9, 10):
        book(client, salon, haircut, local(hour), professional=ana)
        book(client, salon, haircut, local(hour, 30), professional=ana)
    # Four tried, three held: the fourth waits for the shop to answer.
    assert Appointment.objects.count() == 3


def test_the_form_says_the_answer_comes_by_whatsapp_to_that_phone(client, salon, ana, haircut, now):
    """The answer reaches every client with a phone, box or no box: the
    business writes from its own WhatsApp. The box is only Kyo's receipt."""
    body = client.get(page(salon), {
        'paso': 'datos', 'day': MONDAY.isoformat(), 'start': local(9).isoformat(),
    }).content.decode()

    assert 'te responde por WhatsApp a este número' in body
    assert 'confirme o rechace' not in body


def test_the_booking_form_is_rate_limited(client, salon, ana, haircut, now):
    """It writes rows and queues paid messages to any number typed."""
    codes = [book(client, salon, haircut, local(3), professional=ana).status_code
             for _ in range(11)]

    assert codes[-1] == 429


# --- the shop's answer ----------------------------------------------------------

@pytest.fixture
def request_for_ana(client, salon, ana, haircut, now):
    book(client, salon, haircut, local(9), professional=ana)
    OutboundMessage.objects.all().delete()
    Notification.objects.all().delete()
    return Appointment.objects.get()


def test_accepting_leaves_the_answer_owed_to_the_client_not_to_kyo(
    request_for_ana, coordinator, salon, ana,
):
    """The answer goes from the business's own WhatsApp (the step in the turno
    sheet), so Kyo's number sends nothing: the client would get it twice."""
    response = act(coordinator, salon, request_for_ana, 'confirm')

    assert response.json()['answer_notice'] == 'accepted'
    assert not OutboundMessage.objects.exists()
    note = Notification.objects.get()
    assert (note.recipient, note.verb) == (ana, Notification.Verb.APPOINTMENT_CONFIRMED)


def test_accepting_your_own_request_is_not_news_to_you(request_for_ana, salon, ana):
    act(ana.user, salon, request_for_ana, 'confirm')

    assert not Notification.objects.exists()


def test_turning_a_request_down_leaves_the_rejection_owed(request_for_ana, coordinator, salon, ana):
    response = act(coordinator, salon, request_for_ana, 'cancel', reason='Ese día cerramos')

    assert response.json()['answer_notice'] == 'rejected'
    assert response.json()['cancellation_reason'] == 'Ese día cerramos'
    assert not OutboundMessage.objects.exists()
    assert Notification.objects.get().verb == Notification.Verb.APPOINTMENT_REJECTED


def test_calling_off_an_accepted_turno_drops_the_acceptance_owed(request_for_ana, coordinator, salon):
    act(coordinator, salon, request_for_ana, 'confirm')

    response = act(coordinator, salon, request_for_ana, 'cancel')

    assert response.json()['answer_notice'] == ''


def test_telling_the_client_clears_what_is_owed(request_for_ana, coordinator, salon):
    act(coordinator, salon, request_for_ana, 'confirm')

    first = act(coordinator, salon, request_for_ana, 'answer-told')
    # A colleague told them first: still fine, never an error.
    again = act(coordinator, salon, request_for_ana, 'answer-told')

    assert first.status_code == 200 and first.json()['answer_notice'] == ''
    assert again.status_code == 200


def test_only_who_may_act_on_the_turno_marks_it_told(request_for_ana, salon, ana, bea):
    request_for_ana.confirm()

    assert act(bea.user, salon, request_for_ana, 'answer-told').status_code == 403
    assert act(ana.user, salon, request_for_ana, 'answer-told').status_code == 200


def test_a_late_kyo_payment_never_stops_telling_a_client(request_for_ana, coordinator, salon):
    request_for_ana.confirm()
    Tenant.objects.filter(pk=salon.pk).update(paid_until=date(2020, 1, 1))

    assert act(coordinator, salon, request_for_ana, 'answer-told').status_code == 200


def test_a_request_the_sweep_expires_owes_no_answer(request_for_ana, now, settings):
    settings.VAPID_PRIVATE_KEY = 'k'  # else the command raises, as on CI
    settings.VAPID_SUBJECT = 'mailto:x@example.com'
    now(local(9, 1))

    call_command('send_reminders')

    request_for_ana.refresh_from_db()
    assert request_for_ana.status == Appointment.Status.CANCELLED
    assert request_for_ana.answer_notice == ''


def test_cancelling_a_confirmed_turno_is_unchanged(request_for_ana, coordinator, salon, ana):
    request_for_ana.confirm()

    act(coordinator, salon, request_for_ana, 'cancel')

    assert not OutboundMessage.objects.exists()
    assert Notification.objects.get().verb == Notification.Verb.APPOINTMENT_CANCELLED


def test_a_request_is_pushed_as_a_request_not_a_cancellation(request_for_ana, coordinator,
                                                              salon):
    """Every verb but the digest used to go out titled "Turno cancelado"."""
    from apps.scheduling.management.commands.send_reminders import Command

    book_note = Notification(verb=Notification.Verb.APPOINTMENT_REQUESTED,
                             appointment=request_for_ana, recipient=request_for_ana.professional)
    act(coordinator, salon, request_for_ana, 'confirm')
    confirm_note = Notification.objects.get()

    assert Command().notification_payload(book_note)['title'] == 'Nuevo pedido de turno'
    payload = Command().notification_payload(confirm_note)
    assert payload['title'] == 'Turno confirmado'
    assert payload['body'].startswith('Carla confirmó: Corte del 10/08 a las 09:00')


def test_a_request_whose_hour_has_passed_cannot_be_accepted(request_for_ana, coordinator, salon, now):
    now(local(9, 1))

    res = act(coordinator, salon, request_for_ana, 'confirm')

    assert (res.status_code, res.data['code']) == (400, 'request_expired')
    request_for_ana.refresh_from_db()
    assert request_for_ana.status == Appointment.Status.PENDING
    # Turning it down still works.
    assert act(coordinator, salon, request_for_ana, 'cancel').status_code == 200
