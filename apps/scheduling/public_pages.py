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
"""

import re
from datetime import timedelta
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

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
from apps.scheduling import whatsapp
from apps.scheduling.models import Service, WorkSchedule
from apps.scheduling.public import (
    BookingRequestSerializer,
    _shop,
    create_booking,
    offered_slots,
)
from apps.scheduling.views import Overlaps

# Two weeks. Long enough that somebody booking a colour in advance finds a day,
# short enough that the page stays one scroll and one query.
HORIZON_DAYS = 13

# Where each field's error points, from the summary at the top of the form.
ERROR_ANCHORS = {'start': 'cuando', 'name': 'f-name', 'phone': 'f-phone', 'email': 'f-email'}

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

    services = list(Service.objects.filter(tenant=shop))
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
            errors = form.errors

    today = timezone.localdate(timezone=zone)
    offered = offered_slots(
        shop, service, professional, today, today + timedelta(days=HORIZON_DAYS),
    )
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
    first_slot = open_days[0]['slots'][0] if open_days else None

    def href(**changes):
        params = {
            'service': service.pk,
            'professional': professional.pk if professional else '',
            'day': picked_day,
            **changes,
        }
        return '?' + urlencode(params)

    for option in services:
        option.href = href(service=option.pk) + '#servicio'
        option.price_label = _money(option.price)
    for n, one in enumerate(professionals):
        # Which of the six tints names this person. Same rule as the staff
        # panel, so a professional keeps one colour on both sides of the product.
        one.hue = n % 6 + 1
        one.initials = _initials(one.display_name())
        one.href = href(professional=one.pk) + '#profesional'
    for one in days:
        one['href'] = href(day=one['date'].isoformat()) + '#cuando'

    return _render(shop, {
        'services': services,
        'service': service,
        'service_price': _money(service.price),
        'professionals': professionals,
        'professional': professional,
        'anyone_href': href(professional='') + '#profesional',
        # "Cualquiera" names nobody, so it wears no tint: the selection is drawn
        # in ink, as every other control is.
        'hue': professional.hue if professional else 0,
        'days': days,
        'day': day,
        'periods': _periods(day['slots'] if day else [], zone),
        # The one fact somebody arrives for. It leads the page, and one tap on
        # it lands on the form with that hour already ticked.
        'first_slot': first_slot,
        'first_href': href(
            day=first_slot.astimezone(zone).date().isoformat(), start=first_slot.isoformat(),
        ) + '#datos' if first_slot else '',
        'first_is_today': bool(first_slot) and first_slot.astimezone(zone).date() == today,
        'picked_start': asked.get('start', ''),
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
        'booked_at': _booked_at(request, zone),
        'whatsapp_link': _whatsapp_link(shop),
        'opening_hours': _opening_hours(professionals),
    })


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
        {'shop': shop, 'shop_timezone': shop.timezone, **context},
        template_name='scheduling/booking.html',
    )


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
    days = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')
    return [
        {'day': days[weekday], 'opens': opens, 'closes': closes}
        for weekday, (opens, closes) in sorted(by_weekday.items())
    ]


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
