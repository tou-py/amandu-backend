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
