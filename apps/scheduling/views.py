from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import mixins, viewsets
from rest_framework.permissions import IsAuthenticated

from apps.accounts.models import Membership, Notification
from apps.scheduling.models import Appointment, AppointmentTemplate, Category, Client, Service
from apps.scheduling.permissions import OwnsAppointmentOrActsForTheTeam
from apps.scheduling.serializers import (
    AppointmentCancelSerializer,
    AppointmentSerializer,
    AppointmentTemplateSerializer,
    AttendanceSerializer,
    CategorySerializer,
    ClientSerializer,
    GenerateOccurrencesSerializer,
    ProfessionalSerializer,
    ServiceSerializer,
)
from apps.tenancy.permissions import HasActiveMembership
from apps.tenancy.viewsets import TenantScopedModelViewSet


class ClientViewSet(TenantScopedModelViewSet):
    queryset = Client.objects.all()
    serializer_class = ClientSerializer


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
class AppointmentViewSet(TenantScopedModelViewSet):
    permission_classes = (IsAuthenticated, HasActiveMembership, OwnsAppointmentOrActsForTheTeam)
    queryset = (
        Appointment.objects
        .select_related('professional__user', 'service', 'template')
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
        response = self._transition(appointment, appointment.complete)
        # The one automatic trigger in the whole templates feature, and it is
        # not automatic in the cron sense: it only fires as the direct,
        # synchronous consequence of this human clicking "complete".
        template = appointment.template
        if (
            template is not None
            and template.status == AppointmentTemplate.Status.ACTIVE
            and template.auto_generate_on_complete
        ):
            template.generate_occurrences(count=1)
        return response

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


class AppointmentTemplateViewSet(TenantScopedModelViewSet):
    """CRUD for the recurring shape, plus the one action that turns it into
    real bookings. OwnsAppointmentOrActsForTheTeam applies unchanged: it only
    reads obj.professional_id and can_schedule_for_others(), both of which
    AppointmentTemplate has exactly like Appointment."""

    permission_classes = (IsAuthenticated, HasActiveMembership, OwnsAppointmentOrActsForTheTeam)
    queryset = (
        AppointmentTemplate.objects
        .select_related('professional__user', 'service')
        .prefetch_related('clients')
    )
    serializer_class = AppointmentTemplateSerializer

    @extend_schema(request=GenerateOccurrencesSerializer, responses=AppointmentSerializer(many=True))
    @action(detail=True, methods=['post'])
    def generate(self, request, pk=None):
        template = self.get_object()
        body = GenerateOccurrencesSerializer(data=request.data)
        body.is_valid(raise_exception=True)

        result = template.generate_occurrences(count=body.validated_data['count'])
        return Response({
            'created': AppointmentSerializer(
                result['created'], many=True, context=self.get_serializer_context()
            ).data,
            'skipped': result['skipped'],
        })
