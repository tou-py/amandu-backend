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

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from phonenumber_field.serializerfields import PhoneNumberField
from rest_framework import serializers

from apps.accounts.models import Membership
from apps.scheduling.announce import REQUESTED, announce
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
    # Absent or empty is "Cualquiera": the shop assigns whoever is free.
    professional = serializers.PrimaryKeyRelatedField(
        queryset=Membership.objects.none(), required=False, allow_null=True,
    )
    start = serializers.DateTimeField()
    name = serializers.CharField(max_length=120)
    phone = PhoneNumberField()
    email = serializers.EmailField(required=False, allow_blank=True)
    # The "Avisarme por WhatsApp" box. An unticked checkbox is simply absent
    # from a form post, hence the default.
    notify_whatsapp = serializers.BooleanField(required=False, default=False)

    def __init__(self, *args, shop=None, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped to this tenant before any id is looked at, so a guessed id from
        # another shop resolves to nothing rather than to somebody else's row.
        self.fields['service'].queryset = Service.objects.filter(tenant=shop)
        self.fields['professional'].queryset = Membership.professionals_for(shop)
        # Same per-tenant region as ClientSerializer: "0981 123456" means a
        # Paraguayan mobile only because this shop is in Paraguay.
        if shop is not None and shop.country:
            self.fields['phone'].region = shop.country
        self.shop = shop

    def to_internal_value(self, data):
        # The form's "Cualquiera" chip posts an empty value, which a related
        # field would otherwise reject as an invalid pk.
        if hasattr(data, 'get') and data.get('professional') in ('', 'any'):
            data = data.copy()
            data.pop('professional')
        return super().to_internal_value(data)

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
        if not candidates(self.shop, attrs['service'], attrs.get('professional'), start):
            raise serializers.ValidationError(
                {'start': 'That slot is not available.'}
            )
        # Every pending request holds a real slot in somebody's diary, and a
        # stranger proved nothing by typing a phone number. Past this many, the
        # shop has to answer before the same phone may ask again.
        waiting = Appointment.objects.filter(
            tenant=self.shop, status=Appointment.Status.PENDING,
            clients__phone=attrs['phone'],
        ).count()
        if waiting >= MAX_PENDING_PER_PHONE:
            raise serializers.ValidationError({
                'phone': f'Ya tenés {waiting} pedidos esperando respuesta. '
                         'Esperá a que te contesten antes de pedir otro.',
            })
        return attrs


MAX_PENDING_PER_PHONE = 3


def offered_slots(shop, service, professional, since, until):
    """
    The free slots the page offers: one professional's, or with "Cualquiera"
    (None) every slot at least one of them has free.

    ponytail: one free_slots() per professional. Fine for a salon's team;
    batch it if a tenant ever has dozens who attend.
    """
    if professional is not None:
        return free_slots(professional, service, since, until)
    merged = {}
    for one in Membership.professionals_for(shop):
        for day, slots in free_slots(one, service, since, until).items():
            merged.setdefault(day, set()).update(slots)
    return {day: sorted(slots) for day, slots in sorted(merged.items())}


def candidates(shop, service, professional, start):
    """
    Who could take `start`, in the order to try them.

    A named professional is the only candidate. "Cualquiera" is everyone free
    at that instant, least booked that day first so requests spread over the
    team instead of all landing on whoever is listed first, then by id so the
    order never depends on the database's mood.
    """
    day = start.astimezone(ZoneInfo(shop.timezone)).date()
    if professional is not None:
        pool = [professional]
    else:
        pool = sorted(
            Membership.professionals_for(shop),
            key=lambda one: (_load(one, day, shop), one.pk),
        )
    return [one for one in pool if start in free_slots(one, service, day, day)[day]]


def _load(professional, day, shop):
    # The shop's day, not the server's: start__date would cut it at UTC midnight.
    opens = datetime.combine(day, time.min, tzinfo=ZoneInfo(shop.timezone))
    return Appointment.objects.filter(
        professional=professional,
        start__gte=opens,
        start__lt=opens + timedelta(days=1),
    ).exclude(status=Appointment.Status.CANCELLED).count()


def create_booking(shop, data):
    """
    Turn validated booking input into a pending appointment: the shop decides
    whether it becomes real.
    """
    with transaction.atomic():
        client = _client_for(shop, data)
        for professional in candidates(
            shop, data['service'], data.get('professional'), data['start'],
        ):
            appointment = Appointment(
                tenant=shop,
                professional=professional,
                service=data['service'],
                start=data['start'],
                end=data['start'] + data['service'].duration,
                status=Appointment.Status.PENDING,
                source=Appointment.Source.PUBLIC,
            )
            try:
                # Savepoint of its own: the overlap constraint is the last
                # defence against two strangers submitting the same slot in the
                # same instant, and catching it has to leave the transaction
                # usable -- with "Cualquiera", to try the next person.
                with transaction.atomic():
                    appointment.save()
                break
            except IntegrityError as exc:
                if 'no_overlap_per_professional' not in str(exc):
                    raise
        else:
            raise Overlaps()
        appointment.client_links.create(client=client)
        # Inside the transaction: a request the shop is never told about is
        # worse than no request at all, so it is not allowed to exist without
        # its notices.
        announce(appointment, REQUESTED)
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
    # Consent only ever goes ON from here. Someone typing a known number can at
    # worst send that number the notices of a request it did not make -- kept
    # small by the rate limit and MAX_PENDING_PER_PHONE -- but can never
    # silence a regular who asked to be told.
    if data.get('notify_whatsapp') and not client.whatsapp_opt_in:
        client.whatsapp_opt_in = True
        client.save(update_fields=['whatsapp_opt_in', 'updated_at'])
    return client
