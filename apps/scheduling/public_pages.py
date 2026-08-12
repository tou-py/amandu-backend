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

from datetime import timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from django.shortcuts import redirect
from django.utils import timezone
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
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.models import Membership
from apps.scheduling.availability import free_slots
from apps.scheduling.models import Service, WorkSchedule
from apps.scheduling.public import BookingRequestSerializer, _shop, create_booking

# Two weeks. Long enough that somebody booking a colour in advance finds a day,
# short enough that the page stays one scroll and one query.
HORIZON_DAYS = 13

# The page speaks Spanish because its readers do. Activated by a {% language %}
# block INSIDE the template, not around this Response: DRF renders a
# TemplateHTMLRenderer response after the view has already returned, so a
# translation.override() here would have exited before a single day name was
# formatted, and the page would come back reading "Wednesday 12 de August".
# Not by moving LANGUAGE_CODE either, which would also translate the API's
# error messages to a front end that does not expect it.


class PublicPageThrottle(ScopedRateThrottle):
    """
    Reading the page and asking for a slot are the same URL and must not share
    a budget: one is browsing, the other writes a row into a real diary.
    """

    def allow_request(self, request, view):
        self.scope = 'public-booking' if request.method == 'POST' else 'public-read'
        return super().allow_request(request, view)


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

    service = _pick(services, request.query_params.get('service'))
    professional = _pick(professionals, request.query_params.get('professional'))

    errors = {}
    if request.method == 'POST':
        form = BookingRequestSerializer(data=request.data, shop=shop)
        if form.is_valid():
            booked = create_booking(shop, form.validated_data)
            # Redirect after POST so a refresh does not book a second slot. The
            # time travels in the query so the confirmation can name it; it is a
            # time and not an identifier, so nothing here is a handle on the
            # booking for whoever reads the URL over a shoulder.
            #
            # urlencode and not an f-string: an ISO instant ends in '+00:00',
            # and a raw '+' in a query string decodes back as a space, so the
            # confirmation would 400 on the timestamp it just wrote itself.
            return redirect('{}?{}'.format(request.path, urlencode({
                'service': booked.service_id,
                'professional': booked.professional_id,
                'at': booked.start.isoformat(),
            })))
        errors = form.errors
        service = _pick(services, request.data.get('service')) or service
        professional = _pick(professionals, request.data.get('professional')) or professional

    today = timezone.localdate(timezone=zone)
    week = [
        {'date': day, 'slots': slots}
        for day, slots in free_slots(
            professional, service, today, today + timedelta(days=HORIZON_DAYS),
        ).items()
    ]
    opening_hours = _opening_hours(professionals)
    first_slot = next((slot for day in week for slot in day['slots']), None)

    return _render(shop, {
        'services': services,
        'service': service,
        'professionals': professionals,
        'professional': professional,
        # Which of the six tints names this stylist. Same rule as the staff
        # panel, so a person keeps one colour on both sides of the product.
        'hue': professionals.index(professional) % 6 + 1,
        'week': week,
        'has_any_slot': any(day['slots'] for day in week),
        # The one fact somebody arrives for. It leads the page, so it is worth
        # its own name in the context rather than being dug out of `week`.
        'first_slot': first_slot,
        'first_is_today': bool(first_slot) and first_slot.astimezone(zone).date() == today,
        'open_until': _open_until(professionals, zone),
        'errors': errors,
        'submitted': request.data if errors else {},
        'booked_at': _booked_at(request, zone),
        'opening_hours': opening_hours,
    })


def _booked_at(request, zone):
    """The slot just booked, for the confirmation. Absent on a normal visit."""
    raw = request.query_params.get('at')
    if not raw:
        return None
    parsed = serializers.DateTimeField(required=False).run_validation(raw)
    return parsed.astimezone(zone)


def _render(shop, context):
    return Response(
        {'shop': shop, 'shop_timezone': shop.timezone, **context},
        template_name='scheduling/booking.html',
    )


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
