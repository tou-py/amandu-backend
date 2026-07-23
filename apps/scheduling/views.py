from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.scheduling.models import Appointment, Category, Client, Service
from apps.scheduling.serializers import (
    AppointmentSerializer,
    CategorySerializer,
    ClientSerializer,
    ServiceSerializer,
)
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


class AppointmentViewSet(TenantScopedModelViewSet):
    queryset = Appointment.objects.select_related('professional', 'service').prefetch_related('clients')
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

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        appointment = self.get_object()
        return self._transition(
            appointment, lambda: appointment.cancel(request.data.get('reason', ''))
        )

    @action(detail=True, methods=['post'])
    def complete(self, request, pk=None):
        appointment = self.get_object()
        return self._transition(appointment, appointment.complete)

    @action(detail=True, methods=['post'])
    def no_show(self, request, pk=None):
        appointment = self.get_object()
        return self._transition(appointment, appointment.mark_no_show)
