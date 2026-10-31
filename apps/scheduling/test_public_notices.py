"""
A public request and its answers, from the outside: what the page books, who
hears about it, and what the sweep actually sends.

Driven through the highest seams there are -- the public page's POST, the
confirm/cancel actions, and `send_reminders` -- with exactly one fake: the call
to WAHA (`whatsapp.send`), and the pauses that pace it.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.core.management import call_command
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.scheduling import whatsapp
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
def waha(settings):
    settings.WAHA_URL = 'http://waha.test'
    settings.WAHA_API_KEY = 'key'
    settings.WHATSAPP_NUMBER = '+595981000000'


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
        public_booking=True, phone='+595211234567',
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


@pytest.fixture
def number(monkeypatch):
    """What WAHA says of the number's limits; a test edits it to restrict it."""
    state = {'timelock': {'isActive': False}, 'capping': {'cappingStatus': 'NONE'}}

    class Reply:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            if state.get('down'):
                raise ConnectionError('WAHA is down')

        def json(self):
            return state[self.url.rsplit('/', 1)[1]]

    monkeypatch.setattr(whatsapp.requests, 'get', lambda url, headers, timeout: Reply(url))
    return state


@pytest.fixture
def sent(monkeypatch, number):
    """Stands in for WAHA: records (to, text), or raises if told to."""
    calls = []

    def fake(to, text):
        if calls and calls[-1] == 'fail':
            raise ConnectionError('WAHA is down')
        calls.append((str(to), text))

    monkeypatch.setattr(whatsapp, 'send', fake)
    monkeypatch.setattr(whatsapp, '_pause', lambda seconds: None)
    return calls


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

    body = client.get(page(salon)).content.decode()

    assert 'Cualquiera' in body
    # Ana is busy at 09:00 but Bea is not: the slot is still on offer.
    assert local(9).isoformat() in body


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


def test_without_waha_nothing_is_queued(client, salon, ana, haircut, now, settings):
    settings.WAHA_URL = ''

    book(client, salon, haircut, local(9), professional=ana)

    assert not OutboundMessage.objects.exists()


def test_one_phone_cannot_stack_requests(client, salon, ana, haircut, now):
    for hour in (9, 10):
        book(client, salon, haircut, local(hour), professional=ana)
        book(client, salon, haircut, local(hour, 30), professional=ana)
    # Four tried, three held: the fourth waits for the shop to answer.
    assert Appointment.objects.count() == 3


def test_the_confirmation_offers_to_write_first(client, salon, ana, haircut, now):
    response = book(client, salon, haircut, local(9), professional=ana)
    body = client.get(response['Location']).content.decode()

    assert 'https://wa.me/595981000000' in body


def test_the_form_says_the_answer_comes_by_whatsapp_to_that_phone(client, salon, ana, haircut, now):
    """The answer reaches every client with a phone, box or no box: the
    business writes from its own WhatsApp. The box is only Kyo's receipt."""
    body = client.get(page(salon)).content.decode()

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


def test_a_request_the_sweep_expires_owes_no_answer(request_for_ana, sent, now, settings):
    settings.WAHA_URL = ''  # about the mark, not the delivery
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


# --- the sweep ------------------------------------------------------------------

def queue(salon, count):
    for n in range(count):
        OutboundMessage.objects.create(tenant=salon, to=f'+59598100000{n}', body=f'm{n}')


def test_the_sweep_sends_a_few_per_run(salon, sent, now, settings):
    settings.VAPID_PRIVATE_KEY = ''  # WhatsApp does not wait for push
    now(local(10))
    queue(salon, whatsapp.PER_TICK + 1)

    with pytest.raises(Exception):  # still loud about push not being set up
        call_command('send_reminders')

    assert len(sent) == whatsapp.PER_TICK
    assert OutboundMessage.objects.filter(sent_at__isnull=True).count() == 1


def test_the_sweep_stops_at_the_first_failure(salon, sent, now, settings):
    settings.VAPID_PRIVATE_KEY = 'k'
    settings.VAPID_SUBJECT = 'mailto:x@example.com'
    now(local(10))
    queue(salon, 2)
    sent.append('fail')

    call_command('send_reminders')

    failed = OutboundMessage.objects.get(attempts=1)
    assert 'WAHA is down' in failed.last_error and failed.sent_at is None
    assert OutboundMessage.objects.get(attempts=0)  # never tried this run


def test_a_message_is_given_up_after_max_attempts(salon, sent, now):
    now(local(10))
    OutboundMessage.objects.create(
        tenant=salon, to=CLIENT_PHONE, body='x', attempts=whatsapp.MAX_ATTEMPTS,
    )

    assert whatsapp.deliver_due() == (0, False)


def test_nothing_goes_out_at_night(salon, sent, now):
    now(local(23))
    queue(salon, 1)

    assert whatsapp.deliver_due() == (0, False)
    assert sent == []


def test_one_number_gets_at_most_a_few_an_hour(salon, sent, now):
    now(local(10))
    for n in range(whatsapp.PER_HOUR):
        OutboundMessage.objects.create(tenant=salon, to=CLIENT_PHONE, body=f'old{n}',
                                       sent_at=local(9, 30))
    OutboundMessage.objects.create(tenant=salon, to=CLIENT_PHONE, body='new')
    queue(salon, 1)

    assert whatsapp.deliver_due() == (1, False)
    assert [text for _, text in sent] == ['m0']  # the other number still gets its turn

    now(local(10, 31))  # an hour after the last one
    whatsapp.deliver_due()
    assert sent[-1] == (CLIENT_PHONE, 'new')


@pytest.mark.parametrize('limits', [
    {'timelock': {'isActive': True}},
    {'capping': {'cappingStatus': 'FIRST_WARNING'}},
    {'capping': {'cappingStatus': 'CAPPED'}},
])
def test_a_restricted_number_sends_nothing(salon, sent, number, now, limits):
    now(local(10))
    queue(salon, 1)
    number.update(limits)

    assert whatsapp.deliver_due() == (0, False)
    assert sent == []
    assert OutboundMessage.objects.get().attempts == 0  # not a failure: it waits


def test_waha_unable_to_say_is_a_failure(salon, sent, number, now):
    now(local(10))
    queue(salon, 1)
    number['down'] = True

    assert whatsapp.deliver_due() == (0, True)
    assert sent == []


def test_waha_is_asked_to_type_then_send(monkeypatch, settings):
    calls = []

    class Ok:
        def raise_for_status(self):
            pass

    def post(url, json, headers, timeout):
        calls.append((url, json))
        return Ok()

    monkeypatch.setattr(whatsapp.requests, 'post', post)
    monkeypatch.setattr(whatsapp, '_pause', lambda seconds: None)

    whatsapp.send('+595981123456', 'hola')

    assert [url.rsplit('/', 1)[1] for url, _ in calls] == ['startTyping', 'stopTyping', 'sendText']
    assert calls[-1][1] == {'session': 'default', 'chatId': '595981123456@c.us', 'text': 'hola'}


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
