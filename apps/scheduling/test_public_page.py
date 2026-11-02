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

    assert '"@type": "LocalBusiness"' in body
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
    body = html(client, salon, paso='cuando')

    assert 'lunes' in body
    assert 'agosto' in body
    assert 'Monday' not in body.split('</head>')[1]


def test_the_slots_are_drawn_in_the_shops_own_hours(client, salon, stylist, haircut,
                                                    open_monday, monday_morning):
    body = html(client, salon, paso='cuando')

    assert '>09:00<' in body
    assert '>10:30<' in body
    # 11:00 is the closing time, not a slot that fits before it.
    assert '>11:00<' not in body


# --- the band and the bar at the foot -----------------------------------------

@pytest.fixture
def soonest_bar(monkeypatch):
    """The bar is off on the live page for now; these tests keep it working."""
    monkeypatch.setattr('apps.scheduling.public_pages.SOONEST_BAR', True)


def test_the_soonest_bar_is_off_for_now(client, salon, stylist, haircut, open_monday, monday_morning):
    assert 'Próximo turno libre' not in html(client, salon)


def test_the_soonest_free_hour_waits_at_the_foot_of_the_services(client, salon, stylist, haircut, soonest_bar,
                                                                 open_monday, monday_morning):
    """The one fact somebody arrives for, one tap from the form, without
    pushing the services below the fold."""
    body = html(client, salon)

    assert 'Próximo turno libre' in body
    assert 'Hoy, 09:00' in body
    assert 'paso=datos' in body.split('class="dock"')[1]


def test_the_soonest_hour_is_the_shortest_services(client, salon, stylist, haircut, soonest_bar,
                                                   open_monday, monday_morning):
    """It fits in any gap a longer one does: its first hour is the shop's first."""
    Service.objects.create(tenant=salon, name='Color', duration=timedelta(minutes=90))
    Appointment.objects.create(tenant=salon, professional=stylist, service=haircut,
                               start=local(9, 30), end=local(10, 30))

    dock = html(client, salon).split('class="dock"')[1]

    assert 'Hoy, 09:00' in dock
    assert 'Corte' in dock


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

    # The page shows one day's hours at a time, the first open one by default:
    # this Monday, not the next one, which is legitimately free at nine.
    first_day = html(client, salon, paso='cuando')

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
    assert 'Hasta que te confirmen' in body


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
    """Two weeks are drawn, but only the days that have something to offer can
    be picked: the rest are there to keep the fortnight's shape."""
    body = html(client, salon, paso='cuando')

    assert body.count('<a class="d"') == 2
    assert body.count('<span class="d"') == 12


def test_another_day_shows_its_own_hours(client, salon, stylist, haircut, open_monday,
                                         monday_morning):
    next_monday = MONDAY + timedelta(days=7)
    body = html(client, salon, paso='cuando', day=next_monday.isoformat())

    assert local(9, day=next_monday).isoformat() in body
    assert local(9).isoformat() not in body


def test_a_picked_hour_lands_on_the_form_with_it_named(client, salon, stylist, haircut,
                                                      open_monday, monday_morning):
    body = html(client, salon, paso='datos', day=MONDAY.isoformat(), start=local(9).isoformat())

    assert f'<input type="hidden" name="start" value="{local(9).isoformat()}">' in body
    assert 'class="card-box ticket"' in body
    assert '10 de agosto, 09:00' in body


def test_a_form_for_an_hour_not_on_offer_goes_back_to_the_hours(client, salon, stylist, haircut,
                                                                open_monday, monday_morning):
    body = html(client, salon, paso='datos', day=MONDAY.isoformat(), start=local(3).isoformat())

    assert '¿Cuándo?' in body
    assert 'name="phone"' not in body


def test_a_slot_taken_in_the_meantime_is_a_sentence_not_a_409(client, salon, stylist,
                                                               haircut, open_monday,
                                                               monday_morning, monkeypatch):
    """Two strangers on the same slot: the loser keeps what they typed."""
    from apps.scheduling import public_pages
    from apps.scheduling.views import Overlaps

    def taken(*args):
        raise Overlaps()
    monkeypatch.setattr(public_pages, 'create_booking', taken)

    response = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': 'Ada', 'phone': '+595981123456',
    })
    body = response.content.decode()

    assert response.status_code == 200
    assert 'se acaba de ocupar' in body
    assert 'Ada' in body


def test_the_form_errors_are_spanish(client, salon, stylist, haircut, open_monday,
                                     monday_morning):
    body = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': '', 'phone': '+595981123456',
    }).content.decode()

    assert 'role="alert"' in body
    assert 'This field' not in body
    assert 'Falta tu nombre.' in body


# --- images -----------------------------------------------------------------
# Asserted against a literal CDN address, not one rebuilt with the helper: the
# point is what the browser is told to fetch.

@pytest.fixture
def cdn(settings):
    settings.S3_PUBLIC_URL = 'https://img.example.com/'


def test_the_logo_heads_the_page(client, cdn, salon, stylist, haircut, open_monday,
                                 monday_morning):
    """One uploaded before it was measured is drawn as most logos are: a dark
    mark on a clear ground, in a square tile."""
    salon.logo_key = f'{salon.pk}/logo/a.webp'
    salon.save()

    body = html(client, salon)

    assert (
        f'<span class="logo square ink-dark"><img src="https://img.example.com/{salon.pk}/logo/a.webp" '
        'alt="Logo de Salón Iguazú"></span>'
    ) in body


def test_a_measured_logo_is_drawn_to_its_shape_and_on_its_own_ground(client, cdn, salon, stylist,
                                                                     haircut, open_monday,
                                                                     monday_morning):
    salon.logo_key = f'{salon.pk}/logo/a.webp'
    salon.logo_meta = {'aspect': 3.5, 'transparent': False, 'background': '#f3f1ea', 'ink': 'dark'}
    salon.save()

    body = html(client, salon)

    assert '<span class="logo wide ink-dark plated" style="background: #f3f1ea">' in body


def test_a_shop_colour_themes_the_page_and_drops_the_professional_tints(client, salon, stylist,
                                                                        haircut, open_monday,
                                                                        monday_morning):
    assert '.h1, .h2' not in html(client, salon)

    salon.brand_color = '#1f3a5f'
    salon.brand_intensity = 'marcada'
    salon.save()
    body = html(client, salon)

    assert '--accent: #1f3a5f; --accent-ink: #ffffff;' in body
    assert '--band: #1f3a5f; --band-ink: #ffffff;' in body
    assert '<meta name="theme-color" content="#1f3a5f" media="(prefers-color-scheme: light)">' in body
    assert '.h1, .h2, .h3, .h4, .h5, .h6 { --sel-bg: var(--soft)' in body


def test_a_professional_with_a_photo_shows_their_face(client, cdn, salon, stylist,
                                                     haircut, open_monday, monday_morning):
    """The photo takes the initials disc's place at the disc's size. Empty alt:
    the name is printed right beside it, as the disc was aria-hidden."""
    stylist.photo_key = f'{salon.pk}/photo/b.webp'
    stylist.save()

    body = html(client, salon, paso='datos', day=MONDAY.isoformat(), start=local(9).isoformat())

    assert (
        f'<img class="avatar" src="https://img.example.com/{salon.pk}/photo/b.webp" '
        'alt="" width="28" height="28">'
    ) in body
    assert '>AN</span>' not in body


def test_a_professional_without_a_photo_keeps_the_initials(client, cdn, salon, stylist,
                                                          haircut, open_monday,
                                                          monday_morning):
    body = html(client, salon, paso='datos', day=MONDAY.isoformat(), start=local(9).isoformat())

    assert '<span class="avatar" aria-hidden="true">AN</span>' in body
    assert '<img class="avatar"' not in body


def test_a_service_row_shows_its_cover_as_a_taste_and_counts_the_rest(client, cdn, salon, stylist,
                                                                    haircut, open_monday,
                                                                    monday_morning):
    """One link per row, to the service's own page; the first cover fetched at once."""
    haircut.image_key = f'{salon.pk}/service/c.webp'
    haircut.image_2_key = f'{salon.pk}/gallery/d.webp'
    haircut.save()

    row = html(client, salon).split(f'<li class="card" id="svc-{haircut.pk}">')[1].split('</li>')[0]

    assert row.count('<a ') == 1
    assert f'href="?ver={haircut.pk}"' in row
    assert f'<img src="https://img.example.com/{salon.pk}/service/c.webp" alt="" width="120" height="120" fetchpriority="high">' in row
    assert '>+1<' in row
    assert 'gallery/d.webp' not in row


def test_a_service_looked_at_shows_its_pictures_large_and_chooses_it(client, cdn, salon, stylist, bea,
                                                                    haircut, open_monday,
                                                                    monday_morning):
    haircut.image_key = f'{salon.pk}/service/c.webp'
    haircut.image_2_key = f'{salon.pk}/gallery/d.webp'
    haircut.image_ratio = '3:4'
    haircut.description = 'Incluye lavado.'
    haircut.save()

    body = html(client, salon, ver=haircut.pk)

    assert '<title>Corte en Salón Iguazú</title>' in body
    assert '<meta property="og:title" content="Corte en Salón Iguazú">' in body
    assert '<div class="carousel">' in body
    assert body.count('aspect-ratio: 3 / 4') == 2
    assert 'alt="Corte, foto 2 de 2" loading="lazy"' in body
    assert 'Incluye lavado.' in body
    assert 'Elegir este servicio' in body
    assert f'service={haircut.pk}&amp;professional=&amp;day=&amp;paso=profesional' in body.split('class="dock"')[1]
    assert 'Próximo turno libre' not in body
    assert f'href="/reservar/iguazu/#svc-{haircut.pk}"' in body


def test_a_service_that_is_not_the_shops_just_shows_the_list(client, salon, stylist, haircut,
                                                              open_monday, monday_morning):
    other = Tenant.objects.create(name='Otro', slug='otro', timezone='America/Asuncion')
    theirs = Service.objects.create(tenant=other, name='Ajeno', duration=timedelta(minutes=30))

    for asked in (theirs.pk, 'x'):
        body = html(client, salon, ver=asked)
        assert '¿Qué reservás?' in body
        assert 'Elegir este servicio' not in body


# --- one decision per screen --------------------------------------------------

@pytest.fixture
def bea(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='b@example.com', password='pw', first_name='Bea')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


def test_a_service_card_says_what_it_is_and_leads_to_who(client, salon, stylist, bea, haircut,
                                                         open_monday, monday_morning):
    haircut.description = 'Incluye lavado.'
    haircut.save()

    body = html(client, salon)

    assert '¿Qué reservás?' in body
    assert 'Incluye lavado.' in body
    assert f'service={haircut.pk}&amp;professional=&amp;day=&amp;paso=profesional' in html(client, salon, ver=haircut.pk)


def test_a_shop_of_one_skips_choosing_who(client, salon, stylist, haircut, open_monday,
                                          monday_morning):
    services = html(client, salon)
    who = html(client, salon, paso='profesional')

    assert 'paso=cuando' in html(client, salon, ver=haircut.pk)
    assert 'Paso 1 de 3' in services
    assert '¿Qué reservás?' in who


def test_choosing_who_offers_anyone_first(client, salon, stylist, bea, haircut, open_monday,
                                          monday_morning):
    body = html(client, salon, service=haircut.pk, paso='profesional')

    assert '¿Con quién?' in body
    assert body.index('Cualquiera') < body.index('Bea')
    assert 'Paso 2 de 4' in body


def test_services_are_shown_by_category_with_the_unfiled_last(client, salon, stylist, haircut,
                                                              open_monday, monday_morning):
    from apps.scheduling.models import Category
    nails = Category.objects.create(tenant=salon, name='Manos')
    Service.objects.create(tenant=salon, name='Kapping', duration=timedelta(minutes=60), category=nails)

    body = html(client, salon)

    assert '<h3>Manos</h3>' in body
    assert '<h3>Otros servicios</h3>' in body
    assert body.index('Kapping') < body.index('Corte')


# --- after the critique: the end of the journey and the small print --------------

def test_the_confirmation_does_not_offer_another_booking_at_its_foot(client, salon, stylist, haircut,
                                                                    open_monday, monday_morning):
    body = html(client, salon, service=haircut.pk, at=local(9).isoformat())

    assert '¡Pedido enviado!' in body
    assert 'class="dock"' not in body


def test_the_foot_links_to_kyo_only_once_its_landing_is_set(client, salon, settings):
    settings.KYO_LANDING_URL = ''
    assert 'Conocé Kyo' not in html(client, salon)

    settings.KYO_LANDING_URL = 'https://kyo.example/'
    assert '<a href="https://kyo.example/" target="_blank" rel="noopener">' in html(client, salon)


def test_the_soonest_bar_names_the_service_it_books_first(client, salon, stylist, haircut, soonest_bar,
                                                         open_monday, monday_morning):
    dock = html(client, salon).split('class="dock"')[1]

    assert dock.index('<strong>Corte</strong>') < dock.index('Hoy, 09:00')


def test_a_day_link_lands_on_its_hours(client, salon, stylist, haircut, open_monday, monday_morning):
    body = html(client, salon, service=haircut.pk, paso='cuando')

    assert 'paso=cuando#cuando"' in body


def test_a_day_without_hours_says_so_in_words(client, salon, stylist, haircut, open_monday,
                                              monday_morning):
    body = html(client, salon, service=haircut.pk, paso='cuando')

    assert '<span class="sr">, sin horarios</span>' in body


def test_the_last_step_is_under_way_not_done(client, salon, stylist, haircut, open_monday,
                                             monday_morning):
    body = html(client, salon, service=haircut.pk, day=MONDAY.isoformat(),
                start=local(9).isoformat(), paso='datos')

    assert '<i class="now"></i></div>' in body


def test_a_service_without_a_price_says_to_ask(client, salon, stylist, haircut, open_monday,
                                               monday_morning):
    haircut.price = None
    haircut.save()

    assert 'A consultar' in html(client, salon)


def test_a_bad_phone_is_named_in_the_pages_own_voice(client, salon, stylist, haircut, open_monday,
                                                     monday_morning):
    body = client.post(page_url(salon), {
        'service': haircut.pk, 'professional': stylist.pk,
        'start': local(9).isoformat(), 'name': 'Ada', 'phone': '12',
    }).content.decode()

    assert 'Revisá el teléfono: va con el código de área.' in body
    assert 'por ejemplo <span class="amandu-time">0961 456789</span>' in body


# --- who the shop is, and where ----------------------------------------------

def test_a_logo_is_drawn_to_its_shape(client, cdn, salon, stylist, haircut, open_monday, monday_morning):
    salon.logo_key = f'{salon.pk}/logo/a.webp'
    for aspect, shape in ((0.5, 'tall'), (1.2, 'square'), (2.0, 'wide')):
        salon.logo_meta = {'aspect': aspect, 'ink': 'dark'}
        salon.save()
        assert f'<span class="logo {shape} ink-dark">' in html(client, salon)


def test_where_the_shop_is_closes_the_list(client, salon, stylist, haircut, open_monday, monday_morning):
    salon.address = 'Av. España 1240, Asunción'
    salon.instagram = 'iguazu'
    salon.save()

    body = html(client, salon)
    info = body.split('class="info"')[1]

    assert 'Av. España 1240, Asunción</address>' in info
    assert 'https://www.google.com/maps/search/?api=1&amp;query=Av.+Espa%C3%B1a+1240%2C+Asunci%C3%B3n' in info
    assert '@iguazu</a>' in info
    assert 'Hoy: 09:00 a 11:00' in info
    assert '<iframe' not in info
    assert '"address": "Av. Espa\u00F1a 1240, Asunci\u00F3n"' in body
    assert '"sameAs": ["https://www.instagram.com/iguazu/"]' in body


def test_a_shop_that_said_nothing_of_itself_has_no_where_section(client, salon, stylist, haircut,
                                                                 monday_morning):
    assert 'class="info"' not in html(client, salon)
