from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, status, viewsets
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated

from apps.accounts.models import Membership, Notification
from apps.commons.mixins import NoHeuristicCacheMixin
from apps.scheduling.models import (
    Appointment,
    AppointmentClient,
    Category,
    Client,
    ClientField,
    Service,
)
from apps.scheduling.permissions import OwnsAppointmentOrActsForTheTeam
from apps.scheduling.serializers import (
    AppointmentCancelSerializer,
    AppointmentSerializer,
    AttendanceSerializer,
    CategorySerializer,
    ClientFieldSerializer,
    ClientSerializer,
    ProfessionalSerializer,
    ServiceSerializer,
    VisitSerializer,
)
from apps.tenancy.permissions import HasActiveMembership, IsTenantAdmin
from apps.tenancy.viewsets import TenantScopedModelViewSet


class Overlaps(APIException):
    """
    409, not the serializer's 400: nothing was wrong with the request when it was
    made. validate() looked and the slot WAS free -- another transaction took it
    between that look and this insert. That is state, not input, which is the
    same distinction Referenced draws for a protected delete.

    Deliberately the same sentence the serializer raises when it does see the
    clash: one rule, one message, whichever of the two paths gets there first.
    The frontend matches on that text (errors.ts, translateAppointment), so a
    reword here has to be made in both places.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This professional already has an appointment in that time range.'
    default_code = 'overlaps'


class ClientViewSet(TenantScopedModelViewSet):
    """The client file: who they are, whatever this tenant asks about them, and
    every appointment they have ever been on the roster of."""

    queryset = Client.objects.all()
    serializer_class = ClientSerializer

    @extend_schema(responses=VisitSerializer(many=True))
    @action(detail=True, methods=['get'])
    def timeline(self, request, pk=None):
        """
        This client's visits, most recent first, with what was written down on
        each one. Future bookings included: the receptionist opening the file
        wants to know the next appointment as much as the last one.

        A read over rows that already exist -- no new storage. What a visit note
        is today is `Appointment.notes`, one text field per slot, shared by the
        group in it. A note per person, with an author and its own timestamp,
        stays deferred until somebody needs to know who wrote what.

        Paginated: a client of three years has hundreds of these.
        """
        client = self.get_object()
        visits = (
            AppointmentClient.objects
            # Scoped by BOTH sides on purpose. get_object() already proved the
            # client is this tenant's, but a relation traversed from here reaches
            # the appointment table unscoped (TenantOwnedMixin, rule 3), and this
            # is a client file -- the one place a leak would be read as history.
            .filter(client=client, appointment__tenant=request.tenant)
            .select_related('appointment__service', 'appointment__professional__user')
            .order_by('-appointment__start')
        )
        page = self.paginate_queryset(visits)
        serializer = VisitSerializer(page if page is not None else visits, many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)


class ClientFieldViewSet(TenantScopedModelViewSet):
    """
    What this tenant asks about its clients, over and above name and phone.

    Reading is open to any active membership -- the client form cannot be drawn
    without it -- while defining, renaming and removing a field is an owner/admin
    decision: it reshapes the form for the whole business, and a delete throws
    away every answer already given.
    """

    queryset = ClientField.objects.all()
    serializer_class = ClientFieldSerializer
    # Bounded by how many questions a business asks about a client, and read to
    # build a form, so a second page would render half of it.
    pagination_class = None

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.request.method not in SAFE_METHODS:
            permissions.append(IsTenantAdmin())
        return permissions

    def perform_destroy(self, instance):
        """
        Take the answers with the question.

        Left behind, they are keys no definition explains: ClientSerializer
        rejects unknown keys, so every later edit of those clients would 400 on
        data the operator never typed and cannot see. Deleting a field is
        deliberate and rare, and it has to leave the files consistent.

        ponytail: one UPDATE per client holding the key. `has_key` keeps that to
        the clients actually affected; batch it if a tenant ever has enough of
        them for this to be felt.
        """
        with transaction.atomic():
            holders = Client.objects.for_tenant(instance.tenant).filter(
                custom_data__has_key=instance.key
            )
            for client in holders:
                del client.custom_data[instance.key]
                client.save(update_fields=['custom_data', 'updated_at'])
            super().perform_destroy(instance)


class CategoryViewSet(TenantScopedModelViewSet):
    queryset = Category.objects.all()
    serializer_class = CategorySerializer


class ServiceViewSet(TenantScopedModelViewSet):
    queryset = Service.objects.all()
    serializer_class = ServiceSerializer


class ProfessionalViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    The people this tenant's agenda can book, so a client can label and colour a
    slot without asking who each `professional` id belongs to.

    Not a TenantScopedModelViewSet: that base re-scopes through TenantOwnedMixin's
    `for_tenant`, and Membership has no such manager -- it IS the tenant link.
    The scoping is therefore written out here.

    List only. There is no detail route because nothing needs one: the agenda
    reads the whole list once to resolve ids, and memberships are created by
    invitation, never here.

    Unpaginated on purpose. This is a reference list read to resolve ids, so a
    truncated first page would silently mislabel every slot belonging to the
    professionals on page two. It is bounded by a business's staff, not by data.
    """

    serializer_class = ProfessionalSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership)
    pagination_class = None

    def get_queryset(self):
        return (
            Membership.professionals_for(self.request.tenant)
            .select_related('user')
            .order_by('user__first_name', 'user__email')
        )


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter(
                'from',
                OpenApiTypes.DATE,
                description='First calendar day to include (YYYY-MM-DD), read in the '
                            'tenant timezone. Inclusive.',
            ),
            OpenApiParameter(
                'to',
                OpenApiTypes.DATE,
                description='Last calendar day to include (YYYY-MM-DD), read in the '
                            'tenant timezone. Inclusive of the whole day.',
            ),
            OpenApiParameter(
                'professional',
                OpenApiTypes.INT,
                description='Membership id of the professional attending.',
            ),
            OpenApiParameter(
                 'status',
                OpenApiTypes.STR,
                enum=Appointment.Status.values,
                description='Only appointments in this status.',
            ),
        ],
    ),
)
class AppointmentViewSet(NoHeuristicCacheMixin, TenantScopedModelViewSet):
    """
    The agenda: booking slots, moving them, and closing them out.

    A docstring of its own is not decoration. drf-spectacular describes an
    endpoint with `inspect.getdoc(view)`, which walks the MRO -- so without one
    here the public API documentation showed whatever the first base class
    happened to say about its own internals. That is how a note about
    Cache-Control ended up describing "list appointments".
    """

    permission_classes = (IsAuthenticated, HasActiveMembership, OwnsAppointmentOrActsForTheTeam)
    queryset = (
        Appointment.objects
        .select_related('professional__user', 'service')
        # The links, not the clients: the serializer reads attendance off the
        # through row, and prefetching only `clients` would query it per slot.
        .prefetch_related('client_links__client')
    )
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        """
        The agenda, filtered. `from`/`to` are calendar days (YYYY-MM-DD) read in
        the tenant timezone, not UTC (R13): a 23:00 local appointment stored as
        the next UTC day still belongs to its local day. Both ends inclusive.
        """
        queryset = super().get_queryset()
        params = self.request.query_params
        tz = ZoneInfo(self.request.tenant.timezone)

        professional = params.get('professional')
        if professional is not None:
            if not professional.isdigit():
                raise ValidationError({'professional': 'Must be a membership id.'})
            queryset = queryset.filter(professional_id=professional)

        status = params.get('status')
        if status is not None:
            if status not in Appointment.Status.values:
                raise ValidationError({'status': f'Must be one of {Appointment.Status.values}.'})
            queryset = queryset.filter(status=status)

        day_from = self._parse_day(params.get('from'), tz, 'from')
        if day_from is not None:
            queryset = queryset.filter(start__gte=day_from)

        day_to = self._parse_day(params.get('to'), tz, 'to')
        if day_to is not None:
            # Inclusive of the whole 'to' day: up to the next local midnight.
            queryset = queryset.filter(start__lt=day_to + timedelta(days=1))

        return queryset

    # AppointmentSerializer.validate() checks for a clash and cannot hold what it
    # found free: between its .exists() and the INSERT, another transaction can
    # book the same range. no_overlap_per_professional (appointment.py) is what
    # actually stops the double booking, and uncaught it reached the handler as
    # an IntegrityError -- a 500 telling two receptionists working at once that
    # the server broke, when the correct answer is "somebody beat you to it".
    #
    # Both hooks, and only these two: cancel() takes a row OUT of the constraint's
    # condition and complete() leaves its range untouched, so neither can raise it.
    def perform_create(self, serializer):
        self._save_or_conflict(super().perform_create, serializer)

    def perform_update(self, serializer):
        self._save_or_conflict(super().perform_update, serializer)

    @staticmethod
    def _save_or_conflict(save, serializer):
        try:
            # atomic() and not a bare try: a failed statement marks the whole
            # surrounding transaction for rollback, so catching the error and
            # carrying on is only safe from inside a savepoint of its own. It
            # costs nothing today (no ATOMIC_REQUESTS, so this IS the
            # transaction) and is what keeps this correct if that ever changes
            # or a caller wraps a batch of bookings -- which §2.4's recurring
            # series will.
            with transaction.atomic():
                save(serializer)
        except IntegrityError as exc:
            # By constraint name: any other integrity failure here is a real
            # fault and has to keep surfacing as one instead of being dressed up
            # as an ordinary scheduling clash.
            if 'no_overlap_per_professional' not in str(exc):
                raise
            raise Overlaps() from exc

    @staticmethod
    def _parse_day(value, tz, field):
        if value is None:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=tz)
        except ValueError:
            raise ValidationError({field: 'Use YYYY-MM-DD.'})

    def _transition(self, appointment, apply):
        try:
            apply()
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)
        return Response(self.get_serializer(appointment).data)

    @extend_schema(request=AppointmentCancelSerializer, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        appointment = self.get_object()
        # Validated rather than read raw off request.data: `reason` is free text
        # that lands in the record, so it goes through a field like any other.
        body = AppointmentCancelSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        reason = body.validated_data.get('reason', '')
        response = self._transition(appointment, lambda: appointment.cancel(reason))
        # Only a teammate cancelling on someone else's behalf is news to the
        # professional -- cancelling your own slot is not something you need to
        # be told about.
        if request.membership.id != appointment.professional_id:
            Notification.objects.create(
                recipient=appointment.professional,
                actor=request.membership,
                appointment=appointment,
                verb=Notification.Verb.APPOINTMENT_CANCELLED,
            )
        return response

    # No body: the URL already names the transition.
    @extend_schema(request=None, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        appointment = self.get_object()
        return self._transition(appointment, appointment.complete)

    @extend_schema(request=AttendanceSerializer, responses=AppointmentSerializer)
    @action(detail=True, methods=['post'])
    def attendance(self, request, pk=None):
        """
        Record whether ONE person in the slot turned up. Deliberately not a
        transition on the appointment: a booking for four has four answers, and
        the booking's own status has nothing to say about any of them.
        """
        appointment = self.get_object()
        body = AttendanceSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        # .filter on the related manager, not the prefetched cache, so an id that
        # belongs to another appointment cannot be silently accepted.
        link = appointment.client_links.filter(client_id=body.validated_data['client']).first()
        if link is None:
            raise ValidationError({'client': 'That client is not in this appointment.'})

        try:
            link.mark(body.validated_data['attendance'])
        except DjangoValidationError as exc:
            raise ValidationError(exc.messages)

        # Re-read: the instance fetched above carries a prefetched client_links
        # cache still holding the value that was just replaced.
        return Response(self.get_serializer(self.get_object()).data)
