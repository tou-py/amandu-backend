"""
What an unauthenticated stranger may do, shared by the server-rendered booking
page (public_pages.py).

Kept apart from views.py and serializers.py on purpose. This is the only
surface in the product with no credential in front of it, and the question
"what exactly is exposed?" has to be answerable by reading one file rather
than by auditing which of forty viewsets happens to be AllowAny.

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

from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers

from apps.accounts.models import Membership, Notification
from apps.scheduling.availability import free_slots
from apps.scheduling.models import Appointment, Client, Service
from apps.scheduling.views import Overlaps
from apps.tenancy.models import Tenant


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


def create_booking(shop, data):
    """
    Turn validated booking input into a pending appointment: the shop decides
    whether it becomes real.
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
