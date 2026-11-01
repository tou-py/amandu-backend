"""
The booking page a stranger actually lands on, rendered by the server.

Server-rendered and not part of the internal React panel, for two reasons that
both come down to who is reading it. A search engine gets HTML with the shop's
name, services and opening hours already in it, rather than an empty div and a
bundle it has to execute. And the person arriving is on a phone, on cellular,
often on a cheap handset -- the first paint costs them one request, not a
framework.

No JavaScript at all. Every step is a link or a form, which is also why the
booking POST comes back here rather than to the JSON endpoint: both go through
BookingRequestSerializer and both land in create_booking(), so there is one
meaning of "book" and two ways of asking for it.

One decision per screen, in STEPS order, carried in `?paso=`: the service,
who gives it, the day and the hour, then the person's details. Every choice
made so far travels in the query, so a step is a plain GET anybody can go
back to.
"""

import re
from datetime import timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import phonenumbers
from django.conf import settings
from django.shortcuts import redirect
from django.utils import timezone, translation
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import (
    api_view,
    permission_classes,
    renderer_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.renderers import TemplateHTMLRenderer
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from apps.accounts.models import Membership
from apps.commons.images import public_url
from apps.scheduling import whatsapp
from apps.scheduling.models import Service, WorkSchedule
from apps.scheduling.public import (
    BookingRequestSerializer,
    _shop,
    create_booking,
    offered_slots,
)
from apps.scheduling.views import Overlaps
from apps.tenancy import presence
from apps.tenancy.theme import derive_theme

# ponytail: the soonest-hour bar is switched off for now (owner's call,
# 2026-11-01). True brings it back as it was; its tests turn it on themselves.
SOONEST_BAR = False

# Two weeks. Long enough that somebody booking a colour in advance finds a day,
# short enough that the page stays one scroll and one query.
HORIZON_DAYS = 13

# Where each field's error points, from the summary at the top of the form.
ERROR_ANCHORS = {'start': 'cuando', 'name': 'f-name', 'phone': 'f-phone', 'email': 'f-email'}

# The screens, in order, and the question each one asks, in the words of
# somebody at the counter, for any trade.
STEPS = ('servicio', 'profesional', 'cuando', 'datos')
TITLES = {
    'servicio': '¿Qué reservás?',
    'profesional': '¿Con quién?',
    'cuando': '¿Cuándo?',
    'datos': '¿A nombre de quién?',
}
# Django's own messages are in "usted" and do not say which box they mean;
# the page speaks "vos" and names it. A code not listed keeps its words: the
# serializer's own are already written for this page.
SPOKEN = {
    ('name', 'required'): 'Falta tu nombre.',
    ('name', 'blank'): 'Falta tu nombre.',
    ('name', 'max_length'): 'Ese nombre es muy largo.',
    ('phone', 'required'): 'Falta tu teléfono.',
    ('phone', 'blank'): 'Falta tu teléfono.',
    ('phone', 'invalid'): 'Revisá el teléfono: va con el código de área.',
    ('email', 'invalid'): 'Ese email no parece válido. Revisalo o dejalo vacío.',
}
# Past these a logo is no longer a mark that fits a square tile: wider is a
# wordmark, drawn on a line of its own; taller stands in an upright box.
WORDMARK = 1.6
UPRIGHT = 0.8
# The week twice, Monday first as WorkSchedule.weekday counts it: as people
# say it, and as schema.org names it.
WEEKDAYS = ('Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo')
SCHEMA_DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')

# The page speaks Spanish because its readers do. Activated by a {% language %}
# block INSIDE the template, not around this Response: DRF renders a
# TemplateHTMLRenderer response after the view has already returned, so a
# translation.override() here would have exited before a single day name was
# formatted, and the page would come back reading "Wednesday 12 de August".
# Not by moving LANGUAGE_CODE either, which would also translate the API's
# error messages to a front end that does not expect it.


class PublicPageThrottle(SimpleRateThrottle):
    """
    Reading the page and asking for a slot are the same URL and must not share
    a budget: one is browsing, the other writes a row into a real diary and
    queues WhatsApp messages to whatever number was typed.

    SimpleRateThrottle and not ScopedRateThrottle: the scoped one re-reads its
    scope from the view's `throttle_scope`, which a function view does not have,
    so it let every request through no matter what was set here.
    """

    scope = 'public-read'  # read at construction, before any request is seen

    def allow_request(self, request, view):
        self.scope = 'public-booking' if request.method == 'POST' else 'public-read'
        self.num_requests, self.duration = self.parse_rate(self.get_rate())
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


# Out of the OpenAPI schema: it returns HTML to a person, and the schema is
# what the front end generates its typed client from. A page is not an endpoint.
@extend_schema(exclude=True)
@api_view(['GET', 'POST'])
@permission_classes([AllowAny])
@renderer_classes([TemplateHTMLRenderer])
@throttle_classes([PublicPageThrottle])
def booking_page(request, slug):
    shop = _shop(slug)
    zone = ZoneInfo(shop.timezone)

    services = list(Service.objects.filter(tenant=shop).select_related('category'))
    professionals = list(Membership.professionals_for(shop))
    if not services or not professionals:
        # A shop that turned the page on before setting itself up. Saying so is
        # better than an empty picker that looks broken.
        return _render(shop, {'unconfigured': True})

    # A POST carries its choices in the body, a GET in the query; past this
    # point both are just "what was asked for".
    asked = request.data if request.method == 'POST' else request.query_params
    service = _pick(services, asked.get('service'))
    professional = _pick_professional(professionals, asked.get('professional'))

    errors = {}
    if request.method == 'POST':
        form = BookingRequestSerializer(data=request.data, shop=shop)
        # Validated in Spanish here, where it runs; the {% language %} block in
        # the template only covers rendering, which happens after this returns.
        with translation.override('es'):
            valid = form.is_valid()
        if valid:
            try:
                booked = create_booking(shop, form.validated_data)
            except Overlaps:
                # Somebody took the slot between validate() and the insert. On
                # the JSON endpoint that is a 409; on a page it is a sentence
                # next to the hours, with what they typed still in the boxes.
                errors = {'start': ['Ese horario se acaba de ocupar. Elegí otro.']}
            else:
                # Redirect after POST so a refresh does not book a second slot.
                # The time travels in the query so the confirmation can name it;
                # it is a time and not an identifier, so nothing here is a
                # handle on the booking for whoever reads the URL over a
                # shoulder.
                #
                # urlencode and not an f-string: an ISO instant ends in
                # '+00:00', and a raw '+' in a query string decodes back as a
                # space, so the confirmation would 400 on the timestamp it just
                # wrote itself.
                return redirect('{}?{}'.format(request.path, urlencode({
                    'service': booked.service_id,
                    'professional': booked.professional_id,
                    'at': booked.start.isoformat(),
                })))
        else:
            errors = {
                field: [SPOKEN.get((field, getattr(message, 'code', None)), message) for message in messages]
                for field, messages in form.errors.items()
            }

    # A shop of one has nobody to choose between: that step is skipped.
    steps = [one for one in STEPS if one != 'profesional' or len(professionals) > 1]
    step = asked.get('paso') if asked.get('paso') in steps else steps[0]
    if errors:
        step = 'datos'

    today = timezone.localdate(timezone=zone)
    until = today + timedelta(days=HORIZON_DAYS)
    offered = offered_slots(shop, service, professional, today, until) if step in ('cuando', 'datos') else {}
    days = [
        {'date': today + timedelta(days=n), 'slots': offered.get(today + timedelta(days=n), [])}
        for n in range(HORIZON_DAYS + 1)
    ]
    open_days = [day for day in days if day['slots']]
    # One day's hours at a time. Two weeks of every free half hour is a scroll
    # of hundreds of rows on a phone; the day is picked first, as on paper.
    day = next(
        (one for one in open_days if one['date'].isoformat() == asked.get('day')),
        open_days[0] if open_days else None,
    )
    picked_day = day['date'].isoformat() if day else ''
    # The hour asked for, as one of that day's actual slots: a stale or forged
    # `start` sends the person back to the hours rather than to a form for it.
    picked = next((one for one in (day['slots'] if day else []) if one.isoformat() == asked.get('start')), None)
    if step == 'datos' and picked is None and not errors:
        step = 'cuando'

    def href(**changes):
        params = {
            'service': service.pk,
            'professional': professional.pk if professional else '',
            'day': picked_day,
            **changes,
        }
        return '?' + urlencode(params)

    for option in services:
        option.href = href(service=option.pk, day='', paso=steps[1])
        # A look before the choice: its own address, so a shop can also share it.
        option.view_href = '?' + urlencode({'ver': option.pk})
        option.price_label = _money(option.price)
        # The cover and the carousel's other two, all in one ratio.
        option.slides = [
            public_url(key) for key in (option.image_key, option.image_2_key, option.image_3_key) if key
        ]
        option.ratio = option.image_ratio.replace(':', ' / ')
        option.more = len(option.slides) - 1
    categories = _by_category(services)
    # The first cover is the largest paint of the page: fetched at once, not lazily.
    categories[0][1][0].eager = True
    # The service looked at, on the first step only; an id that is not one of
    # this shop's services just shows the list.
    preview = next(
        (one for one in services if str(one.pk) == asked.get('ver')),
        None,
    ) if step == steps[0] and not errors else None
    for n, one in enumerate(professionals):
        # Which of the six tints names this person. Same rule as the staff
        # panel, so a professional keeps one colour on both sides of the product.
        one.hue = n % 6 + 1
        one.initials = _initials(one.display_name())
        one.photo_url = public_url(one.photo_key)
        one.href = href(professional=one.pk, day='', paso='cuando')
    for one in days:
        # Straight to the hours: on a short phone they sit below the fortnight.
        one['href'] = href(day=one['date'].isoformat(), paso='cuando') + '#cuando'
    periods = [
        (name, [{'at': slot, 'href': href(start=slot.isoformat(), paso='datos')} for slot in slots])
        for name, slots in _periods(day['slots'] if day else [], zone)
    ]
    position = steps.index(step)
    booked_at = _booked_at(request, zone)
    opening_hours = _opening_hours(professionals)

    return _render(shop, {
        'step': step,
        'step_number': position + 1,
        'step_count': len(steps),
        'step_title': preview.name if preview else TITLES[step],
        'preview': preview,
        # Done, under way, ahead: the last step is not full before anything is sent.
        'progress': ['on' if n < position else 'now' if n == position else '' for n in range(len(steps))],
        # From a service looked at, back to the list where it was.
        'back_href': href(paso=steps[position - 1]) if position else f'{request.path}#svc-{preview.pk}' if preview else '',
        # The two browsing steps open up on a desktop; the hours and the form
        # keep a phone's measure.
        'wide': step in ('servicio', 'profesional') and not preview,
        'categories': categories,
        'service': service,
        'service_price': _money(service.price),
        'professionals': professionals,
        'professional': professional,
        'anyone_href': href(professional='', day='', paso='cuando'),
        # "Cualquiera" names nobody, so it wears no tint: the selection is drawn
        # in ink, as every other control is.
        'hue': professional.hue if professional else 0,
        'days': days,
        'day': day,
        # The day's hours: links on their step, choices again in the form when
        # the one picked was taken meanwhile.
        'periods': periods,
        'picked': picked,
        # Not on the confirmation, which also lands on the first step: right
        # after a request it would only invite a second one.
        # Nor over a service being looked at, whose own bar chooses it.
        'soonest': _soonest(shop, services, today, until, zone) if SOONEST_BAR and step == 'servicio' and not booked_at and not preview else None,
        'open_until': _open_until(professionals, zone),
        'errors': errors,
        # Each message, with where its field is, for the summary above the form.
        'error_list': [
            (ERROR_ANCHORS.get(field, 'datos'), messages[0])
            for field, messages in errors.items()
        ],
        # The box is a promise; it is only made where something will keep it.
        'whatsapp_enabled': whatsapp.enabled_for(shop),
        'submitted': request.data if errors else {},
        'picked_start': asked.get('start', ''),
        'phone_example': _phone_example(shop),
        'booked_at': booked_at,
        'whatsapp_link': _whatsapp_link(shop),
        'opening_hours': opening_hours,
        'week': _week(opening_hours, today),
    })


def _by_category(services):
    """
    The catalogue as the shop filed it: one run of cards per category, by
    name, with whatever has none last. A shop that never filed anything gets
    one unnamed run rather than a heading that says nothing.
    """
    groups = {}
    for one in services:
        groups.setdefault(one.category.name if one.category else '', []).append(one)
    named = sorted((name, items) for name, items in groups.items() if name)
    if '' in groups:
        named.append(('Otros servicios' if named else '', groups['']))
    return named


def _soonest(shop, services, today, until, zone):
    """
    The soonest hour the shop has free, for the bar at the foot of the
    services, with whoever is free and the service that fits it.

    One service asked, not all: the shortest fits in any gap a longer one
    does, so its first slot is the soonest there is.

    ponytail: on a grid that steps by each service's own length the shortest
    one's first slot can trail another's by a few minutes. It is always a
    real, bookable hour; compute every service if that ever matters.
    """
    shortest = min(services, key=lambda one: one.duration)
    # Days come back empty too: the first one with something in it.
    first = next((slots[0] for slots in offered_slots(shop, shortest, None, today, until).values() if slots), None)
    if first is None:
        return None
    local = first.astimezone(zone)
    return {
        'at': local,
        'is_today': local.date() == today,
        'service': shortest,
        'href': '?' + urlencode({
            'service': shortest.pk, 'professional': '', 'day': local.date().isoformat(),
            'start': first.isoformat(), 'paso': 'datos',
        }),
    }


def _periods(slots, zone):
    """A day's hours in the three blocks people think of a day in."""
    periods = {'Mañana': [], 'Tarde': [], 'Noche': []}
    for slot in slots:
        hour = slot.astimezone(zone).hour
        periods['Mañana' if hour < 12 else 'Tarde' if hour < 19 else 'Noche'].append(slot)
    return [(name, slots) for name, slots in periods.items() if slots]


def _money(amount):
    """Gs. 80.000, as the panel prints it (es-PY). None is a service not charged
    per session, which says nothing rather than 'Gs. 0'."""
    return f'Gs. {amount:,}'.replace(',', '.') if amount is not None else ''


def _phone_example(shop):
    """A mobile number as people write it in the shop's country, for the hint
    under the phone box. None where the country is not set."""
    if not shop.country:
        return None
    number = phonenumbers.example_number_for_type(shop.country, phonenumbers.PhoneNumberType.MOBILE)
    return number and phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL)


def _initials(name):
    """Two letters for the avatar disc, by the panel's own rule (theme.ts)."""
    parts = [part for part in re.split(r'[\s@.]+', name.strip()) if part]
    if not parts:
        return '?'
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _booked_at(request, zone):
    """The slot just booked, for the confirmation. Absent on a normal visit."""
    raw = request.query_params.get('at')
    if not raw:
        return None
    parsed = serializers.DateTimeField(required=False).run_validation(raw)
    return parsed.astimezone(zone)


def _whatsapp_link(shop):
    """
    A wa.me link to the Kyo number, offered once a request is in.

    The single best defence the shared number has against being banned:
    a client who writes first turns every notice after it into a reply, which
    is what WhatsApp expects of an automated number (apps/scheduling/whatsapp.py).
    """
    if not (settings.WHATSAPP_NUMBER and shop.phone):
        return None
    number = settings.WHATSAPP_NUMBER.lstrip('+')
    return f'https://wa.me/{number}?text=' + quote(f'Hola, pedí un turno en {shop.name}.')


def _render(shop, context):
    return Response(
        {
            'shop': shop, 'shop_timezone': shop.timezone,
            'logo': _logo(shop),
            # Kyo's own palette is the stylesheet; only a shop's own colour overrides it.
            'theme': shop.brand_color and derive_theme(shop.brand_color, shop.brand_intensity),
            'kyo_url': settings.KYO_LANDING_URL,
            'socials': presence.social_links(shop),
            'maps_link': presence.maps_link(shop),
            'maps_embed': presence.maps_embed_url(shop),
            'contact': _contact(shop),
            **context,
        },
        template_name='scheduling/booking.html',
    )


def _logo(shop):
    """
    The logo and how to draw it, from what the panel measured on upload
    (Tenant.logo_meta): a wordmark wide and bare, a mark in a square tile; on
    its own ground when it has one. One uploaded before the measurements is
    taken as a dark drawing on a clear ground, as most logos are.
    """
    if not shop.logo_key:
        return None
    meta = shop.logo_meta or {}
    aspect = meta.get('aspect', 1)
    return {
        'url': public_url(shop.logo_key),
        'shape': 'wide' if aspect > WORDMARK else 'tall' if aspect < UPRIGHT else 'square',
        'ink': meta.get('ink', 'dark'),
        # A hex the serializer validated, so it is safe in a style attribute.
        'background': meta.get('background'),
    }


def _pick_professional(professionals, raw):
    """
    The professional asked for, or None for "Cualquiera" -- the default, since
    most people booking a cut do not mind who gives it. A shop of one has no
    choice to offer, so that one person is simply who it is.
    """
    if len(professionals) == 1:
        return professionals[0]
    return next((one for one in professionals if str(one.pk) == str(raw)), None)


def _pick(options, raw):
    """The option whose id was asked for, or the first one. Never a 404: a stale
    link from a service the shop deleted should still show a usable page."""
    for option in options:
        if str(option.pk) == str(raw):
            return option
    return options[0] if options else None


def _opening_hours(professionals):
    """
    The shop's week, for the structured data in the head.

    The union across everyone who attends: what a search engine wants to print
    is when the DOOR is open, which is any hour somebody is working, not one
    person's shift.
    """
    rows = WorkSchedule.objects.filter(professional__in=professionals)
    by_weekday = {}
    for row in rows:
        opens, closes = by_weekday.get(row.weekday, (row.start_time, row.end_time))
        by_weekday[row.weekday] = (
            min(opens, row.start_time), max(closes, row.end_time),
        )
    return [
        {'day': SCHEMA_DAYS[weekday], 'opens': opens, 'closes': closes}
        for weekday, (opens, closes) in sorted(by_weekday.items())
    ]


def _week(opening_hours, today):
    """
    The shop's week as a person reads it, Monday first, with the days it is
    shut said so rather than left out, and today marked for the summary line.

    ponytail: one span per day, lunch breaks merged away as in the structured
    data; list each stretch if a shop's split day ever confuses somebody.
    """
    rows = {row['day']: row for row in opening_hours}
    return [
        {'name': name, 'today': n == today.weekday(), 'hours': rows.get(SCHEMA_DAYS[n])}
        for n, name in enumerate(WEEKDAYS)
    ] if opening_hours else []


def _contact(shop):
    """
    How to reach the shop from its page: WhatsApp when its number is a mobile,
    which in Paraguay is where people write first; a call otherwise.
    """
    if not shop.phone:
        return None
    kind = phonenumbers.number_type(shop.phone)
    if kind in (phonenumbers.PhoneNumberType.MOBILE, phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE):
        return {'kind': 'whatsapp', 'href': f'https://wa.me/{shop.phone.as_e164.lstrip("+")}'}
    return {'kind': 'phone', 'href': f'tel:{shop.phone.as_e164}'}


def _open_until(professionals, zone):
    """
    Closing time if somebody is working right now, otherwise None.

    Answers the first question a stranger has on arriving, and the answer is
    useful either way: open says come by, closed is the whole argument for a
    page that takes bookings while nobody is there to pick up the phone.

    Off the raw stretches and not off _opening_hours, which merges a day down
    to its first opening and last closing. That merge is right for a search
    result and wrong for this: it swallows the lunch break, so a shop shut
    between twelve and two would still be claiming to be open at one.
    """
    now = timezone.localtime(timezone=zone)
    return WorkSchedule.objects.filter(
        professional__in=professionals,
        weekday=now.weekday(),
        start_time__lte=now.time(),
        end_time__gt=now.time(),
        # Whoever is working latest: the door shuts when the last one leaves.
    ).order_by('-end_time').values_list('end_time', flat=True).first()
