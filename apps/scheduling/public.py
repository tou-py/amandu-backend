"""
Everything an unauthenticated stranger can reach.

Deliberately one module rather than rows added to views.py and serializers.py.
This is the only surface in the product with no credential in front of it, and
the question "what exactly is exposed?" has to be answerable by reading one
file rather than by auditing which of forty viewsets happens to be AllowAny.

Three rules hold everywhere below:

1. The tenant comes from the URL slug, never from a header. X-Tenant-ID is the
   authenticated path, where HasActiveMembership proves the caller belongs to
   the tenant it names; here nobody belongs to anything, so the slug is a
   lookup key and every queryset is filtered by the tenant it resolved to.
2. Only a tenant that is operational AND has opted in is visible. Anything else
   is a 404 -- not a 403, which would confirm the shop exists.
3. Nothing about other people leaves. No client list, no client file, no notes,
   no who-is-booked-at-eleven. Availability answers "free or not", and that is
   the whole of it.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.models import Membership, Notification
from apps.scheduling.availability import free_slots
from apps.scheduling.models import Appointment, Client, Service
from apps.scheduling.views import Overlaps
from apps.tenancy.models import Tenant

# How far ahead the public page will look. A stranger asking for slots in 2038
# should not turn into a scan of ten thousand days.
MAX_HORIZON_DAYS = 60


def _shop(slug):
    """
    The tenant behind a public URL, or 404.

    `is_operational` is checked here on purpose: a suspended tenant is one whose
    subscription lapsed, and taking bookings nobody has agreed to honour is
    worse for the person booking than an unavailable page. It does mean a shop
    that is a day late on payment loses its public page -- a product decision
    worth revisiting with a grace period, not a bug.
    """
    shop = get_object_or_404(Tenant, slug=slug, public_booking=True)
    if not shop.is_operational:
        # The same 404 as a shop that does not exist. A distinct status would
        # tell an outsider that this business exists and is behind on payment.
        raise Http404
    return shop


class PublicServiceSerializer(serializers.ModelSerializer):
    duration_minutes = serializers.IntegerField(read_only=True)

    class Meta:
        model = Service
        # No price: the model has none, and when it gets one this list is the
        # first place to decide deliberately whether it goes out.
        fields = ('id', 'name', 'duration_minutes')


class PublicProfessionalSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(source='display_name')


class BookingRequestSerializer(serializers.Serializer):
    """
    What a stranger may send. A Serializer and not a ModelSerializer on purpose:
    a ModelSerializer's field list grows with the model, and this one must only
    ever grow when somebody decides it should.
    """

    service = serializers.PrimaryKeyRelatedField(queryset=Service.objects.none())
    professional = serializers.PrimaryKeyRelatedField(queryset=Membership.objects.none())
    start = serializers.DateTimeField()
    name = serializers.CharField(max_length=120)
    phone = serializers.CharField(max_length=32)
    email = serializers.EmailField(required=False, allow_blank=True)

    def __init__(self, *args, shop=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped to this tenant before any id is looked at, so a guessed id from
        # another shop resolves to nothing rather than to somebody else's row.
        self.fields['service'].queryset = Service.objects.filter(tenant=shop)
        self.fields['professional'].queryset = Membership.professionals_for(shop)
        self.shop = shop

    def validate_start(self, start):
        if start < timezone.now():
            raise serializers.ValidationError('That time has already passed.')
        return start

    def validate(self, attrs):
        """
        The slot has to be one the calculation actually offered.

        Re-derived here rather than trusted from the request: the times the page
        drew came from this same function a minute ago, and a caller who edits
        one by hand is asking to be booked outside opening hours, on a holiday,
        or on top of somebody else. The overlap constraint would catch only the
        last of those three.
        """
        start = attrs['start']
        day = start.astimezone(ZoneInfo(self.shop.timezone)).date()
        offered = free_slots(attrs['professional'], attrs['service'], day, day)[day]

        if start not in offered:
            raise serializers.ValidationError(
                {'start': 'That slot is not available.'}
            )
        return attrs


@extend_schema(
    responses=inline_serializer(
        name='PublicShop',
        fields={
            'name': serializers.CharField(),
            'slug': serializers.SlugField(),
            'timezone': serializers.CharField(),
            'services': PublicServiceSerializer(many=True),
            'professionals': PublicProfessionalSerializer(many=True),
        },
    ),
)
@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def shop_detail(request, slug):
    """What the booking page needs to draw itself before anyone picks a time."""
    shop = _shop(slug)
    return Response({
        'name': shop.name,
        'slug': shop.slug,
        'timezone': shop.timezone,
        'services': PublicServiceSerializer(
            Service.objects.filter(tenant=shop), many=True,
        ).data,
        'professionals': PublicProfessionalSerializer(
            Membership.professionals_for(shop), many=True,
        ).data,
    })


shop_detail.throttle_scope = 'public-read'


@extend_schema(
    parameters=[
        OpenApiParameter('service', int, required=True),
        OpenApiParameter('professional', int, required=True),
        OpenApiParameter('from', OpenApiTypes.DATE, description='Defaults to today.'),
        OpenApiParameter(
            'to', OpenApiTypes.DATE,
            description=f'Defaults to six days out, clamped to {MAX_HORIZON_DAYS}.',
        ),
    ],
    responses=OpenApiResponse(
        # A map keyed by date, so there is no fixed field list to declare.
        response={'type': 'object', 'additionalProperties': {
            'type': 'array', 'items': {'type': 'string', 'format': 'date-time'},
        }},
        description='Free start times per local date, in the tenant timezone.',
    ),
)
@api_view(['GET'])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def availability(request, slug):
    """
    Free start times for one service and one professional over a date range.

    Returns times and nothing else. That a slot is taken is public the moment
    the page renders; WHO is in it is not, and never appears here.
    """
    shop = _shop(slug)

    service = get_object_or_404(Service, pk=request.query_params.get('service'), tenant=shop)
    professional = get_object_or_404(
        Membership.professionals_for(shop), pk=request.query_params.get('professional'),
    )

    today = timezone.localdate(timezone=ZoneInfo(shop.timezone))
    since = _date_param(request, 'from', default=today)
    until = _date_param(request, 'to', default=since + timedelta(days=6))

    if until < since:
        return Response(
            {'detail': '"to" is before "from".'}, status=status.HTTP_400_BAD_REQUEST,
        )
    # Clamped rather than rejected: a page asking for too much wants as much as
    # it can have, and a 400 would only teach it to ask twice.
    until = min(until, since + timedelta(days=MAX_HORIZON_DAYS))

    slots = free_slots(professional, service, since, until)
    return Response({
        day.isoformat(): [moment.isoformat() for moment in moments]
        for day, moments in slots.items()
    })


availability.throttle_scope = 'public-read'


@extend_schema(
    request=BookingRequestSerializer,
    responses={
        201: inline_serializer(
            name='PublicBookingAccepted',
            fields={'detail': serializers.CharField()},
        ),
        409: OpenApiResponse(description='Someone else took the slot first.'),
    },
)
@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([ScopedRateThrottle])
def book(request, slug):
    """
    A stranger asking for a slot. Creates a PENDING appointment: the shop
    decides whether it becomes real.

    The response says only that the request landed. It carries no appointment
    id, no client id and no link -- there is nothing to authenticate whoever
    would use them, so anything handed back here is a handle on a stranger's
    booking for whoever guesses it next. Self-service rescheduling arrives with
    the signed links of roadmap §6, not before.
    """
    shop = _shop(slug)
    form = BookingRequestSerializer(data=request.data, shop=shop)
    form.is_valid(raise_exception=True)
    create_booking(shop, form.validated_data)

    return Response(
        {'detail': 'Your request was sent. The shop will confirm it.'},
        status=status.HTTP_201_CREATED,
    )


book.throttle_scope = 'public-booking'


def create_booking(shop, data):
    """
    Turn validated booking input into a pending appointment.

    Shared by the JSON endpoint above and the server-rendered page, so that the
    two transports cannot drift into two different meanings of "book". Both
    validate through BookingRequestSerializer and both land here.
    """
    with transaction.atomic():
        client = _client_for(shop, data)
        appointment = Appointment(
            tenant=shop,
            professional=data['professional'],
            service=data['service'],
            start=data['start'],
            end=data['start'] + data['service'].duration,
            status=Appointment.Status.PENDING,
            source=Appointment.Source.PUBLIC,
        )
        try:
            # Savepoint of its own: the overlap constraint is the last defence
            # against two strangers submitting the same slot in the same
            # instant, and catching it has to leave the transaction usable.
            with transaction.atomic():
                appointment.save()
        except IntegrityError as exc:
            if 'no_overlap_per_professional' not in str(exc):
                raise
            raise Overlaps() from exc
        appointment.client_links.create(client=client)
        # Inside the transaction: a request the shop is never told about is
        # worse than no request at all, so it is not allowed to exist without
        # its notification. The existing sweep pushes it to their devices.
        Notification.objects.create(
            recipient=appointment.professional,
            actor=None,
            appointment=appointment,
            verb=Notification.Verb.APPOINTMENT_REQUESTED,
        )
    return appointment


def _client_for(shop, data):
    """
    The person booking, reused if this tenant already knows the phone number.

    Reuse and not always-create because the phone IS the client's identity
    inside a tenant (see Client.Meta), so a second booking from the same number
    is the same person and a new row would both violate that constraint and
    split their history in two.

    Their name is NOT overwritten from this payload. A stranger who can guess a
    regular's phone number would otherwise be able to rename them in the shop's
    own files, and nothing here has proved they own the number.
    """
    client, created = Client.objects.get_or_create(
        tenant=shop,
        phone=data['phone'],
        defaults={'name': data['name'], 'email': data.get('email', '')},
    )
    return client


def _date_param(request, name, default):
    raw = request.query_params.get(name)
    if not raw:
        return default
    parsed = serializers.DateField().to_internal_value(raw)
    return parsed
