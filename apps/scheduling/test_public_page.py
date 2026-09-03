"""
The server-rendered booking page.

Asserted on the HTML itself, because the HTML is the product here: a search
engine and a phone on cellular both get exactly what is in this response, with
no second request and no script run.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from apps.accounts.models import Membership
from apps.scheduling.models import Appointment, Client, Service, WorkSchedule
from apps.tenancy.models import Tenant

ASUNCION = ZoneInfo('America/Asuncion')
MONDAY = date(2026, 8, 10)


def local(hour, minute=0, day=MONDAY):
    return datetime.combine(day, time(hour, minute), tzinfo=ASUNCION)


@pytest.fixture
def freeze_to(monkeypatch):
    def freeze(moment):
        monkeypatch.setattr('django.utils.timezone.now', lambda: moment)
    return freeze


@pytest.fixture
def monday_morning(freeze_to):
    freeze_to(local(8))


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        name='Salón Iguazú', slug='iguazu', country='PY',
        timezone='America/Asuncion', public_booking=True,
    )


@pytest.fixture
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(
        email='s@example.com', password='pw', first_name='Ana',
    )
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    return Service.objects.create(tenant=salon, name='Corte', duration=timedelta(minutes=30))


@pytest.fixture
def open_monday(db, salon, stylist):
    return WorkSchedule.objects.create(
        tenant=salon, professional=stylist, weekday=0,
        start_time=time(9), end_time=time(11),
    )


def page_url(shop):
    return reverse('booking-page', args=[shop.slug])


def html(client, shop, **params):
    return client.get(page_url(shop), params).content.decode()


# --- what a search engine gets ----------------------------------------------

def test_the_shop_is_in_the_html_not_fetched_later(client, salon, stylist, haircut,
                                                   open_monday, monday_morning):
    body = html(client, salon)

    assert '<title>Reservar turno en Salón Iguazú</title>' in body
    assert 'Corte' in body


def test_the_opening_hours_become_structured_data(client, salon, stylist, haircut,
                                                  open_monday, monday_morning):
    """The hours the diary enforces are the hours the search result shows, so
    there is no second place to keep them true."""
    body = html(client, salon)

    assert '"@type": "HairSalon"' in body
    assert '"dayOfWeek": "https://schema.org/Monday"' in body
    assert '"opens": "09:00"' in body
    assert '"closes": "11:00"' in body


def test_the_page_needs_no_javascript(client, salon, stylist, haircut, open_monday,
                                      monday_morning):
    """Every step is a link or a form. If a <script> other than the structured
    data ever appears here, the first paint stopped being free."""
    body = html(client, salon)

    assert body.count('<script') == 1
    assert 'application/ld+json' in body


def test_the_page_is_written_in_spanish(client, salon, stylist, haircut, open_monday,
                                        monday_morning):
    """
    LANGUAGE_CODE is en-us for the API's sake, so the page activates Spanish
    itself. It has to do that INSIDE the template: DRF renders a
    TemplateHTMLRenderer response after the view returns, so activating around
    the Response is already over by the time a day name is formatted.
    """
    body = html(client, salon)

    assert 'lunes' in body
    assert 'agosto' in body
    assert 'Monday' not in body.split('</head>')[1]


def test_the_slots_are_drawn_in_the_shops_own_hours(client, salon, stylist, haircut,
                                                    open_monday, monday_morning):
    body = html(client, salon)

    assert '>09:00<' in body
    assert '>10:30<' in body
    # 11:00 is the closing time, not a slot that fits before it.
    assert '>11:00<' not in body


# --- the hero -----------------------------------------------------------------

def test_the_hero_leads_with_the_next_free_slot(client, salon, stylist, haircut,
                                                open_monday, monday_morning):
    """The one fact somebody arrives for, above everything they have to choose."""
    body = html(client, salon)

    assert 'Lo más cercano' in body
    assert 'Hoy a las 09:00' in body


def test_the_badge_says_open_while_somebody_is_working(client, salon, stylist, haircut,
                                                       open_monday, freeze_to):
    freeze_to(local(10))

    assert 'Abierto hasta las 11:00' in html(client, salon)


def test_the_badge_does_not_claim_open_over_the_lunch_break(client, salon, stylist,
                                                            haircut, freeze_to):
    """
    A day is two stretches with a gap. The structured data merges them into one
    span because that is what a search result wants; the badge must not, or a
    shut shop invites somebody to walk over at one o'clock.
    """
    for start, end in ((time(9), time(12)), (time(14), time(19))):
        WorkSchedule.objects.create(tenant=salon, professional=stylist, weekday=0,
                                    start_time=start, end_time=end)
    freeze_to(local(13))
    body = html(client, salon)

    assert 'Cerrado ahora' in body
    assert 'Abierto' not in body


# --- what is not shown ------------------------------------------------------

def test_a_tenant_without_the_flag_has_no_page(client, db):
    private = Tenant.objects.create(
        name='Pilates', slug='pilates', country='PY', timezone='America/Asuncion',
    )

    assert client.get(page_url(private)).status_code == 404


def test_a_booked_slot_is_simply_absent_never_named(client, salon, stylist, haircut,
                                                    open_monday, monday_morning):
    taken = Appointment.objects.create(
        tenant=salon, professional=stylist, service=haircut,
        start=local(9), end=local(9, 30),
    )
    taken.client_links.create(client=Client.objects.create(tenant=salon, name='Grace'))

    # Only the first Monday: the horizon spans two weeks, so the Monday after
    # this one is legitimately free at nine and would make this assertion pass
    # or fail for the wrong reason.
    first_day = html(client, salon).split('class="day"')[1]

    assert '>09:00<' not in first_day
    assert '>09:30<' in first_day
    assert 'Grace' not in first_day


def test_a_shop_with_nothing_set_up_says_so(client, db):
    empty = Tenant.objects.create(
        name='Nueva', slug='nueva', country='PY',
        timezone='America/Asuncion', public_booking=True,
    )

    assert 'todavía no cargó' in html(client, empty)


# --- booking through the form -----------------------------------------------

def test_the_form_books_and_redirects(client, salon, stylist, haircut, open_monday,
                                      monday_morning):
    """Redirect after POST, so a refresh on a phone does not ask for a second
    slot."""
    response = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': 'Ada', 'phone': '+595981123456',
    })

    assert response.status_code == 302
    appointment = Appointment.objects.get()
    assert appointment.status == Appointment.Status.PENDING
    assert appointment.source == Appointment.Source.PUBLIC


def test_the_confirmation_names_the_time_that_was_taken(client, salon, stylist,
                                                        haircut, open_monday,
                                                        monday_morning):
    response = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': 'Ada', 'phone': '+595981123456',
    }, follow=True)
    body = response.content.decode()

    assert '09:00' in body
    assert 'Te confirman ellos' in body


def test_the_confirmation_url_carries_no_identifier(client, salon, stylist, haircut,
                                                    open_monday, monday_morning):
    """A time is not a handle on somebody's booking. An id would be."""
    response = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': 'Ada', 'phone': '+595981123456',
    })
    appointment = Appointment.objects.get()

    assert str(appointment.pk) not in response['Location']


def test_a_rejected_form_comes_back_with_the_reason_and_the_typing(client, salon,
                                                                   stylist, haircut,
                                                                   open_monday,
                                                                   monday_morning):
    """Losing what somebody typed on a phone is how a booking gets abandoned."""
    body = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(3).isoformat(), 'name': 'Ada', 'phone': '+595981123456',
    }).content.decode()

    assert 'Ada' in body
    assert not Appointment.objects.exists()


def test_a_day_with_no_hours_is_left_out(client, salon, stylist, haircut, open_monday,
                                         monday_morning):
    """Two weeks are rendered but only the days that have something to offer."""
    body = html(client, salon)

    assert body.count('class="day"') == 2
