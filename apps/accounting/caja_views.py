from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounting.cajas import saldo
from apps.accounting.models import Caja, MethodCaja
from apps.accounting.serializers import (
    CajaNameSerializer,
    CajaSerializer,
    CajaSetupSerializer,
    MethodMapSerializer,
)
from apps.commons.errors import Refused
from apps.scheduling.billing import local_today
from apps.tenancy import pricing
from apps.tenancy.models import Tenant
from apps.tenancy.permissions import HasActiveMembership, HasCaja, IsTenantAdmin, IsTenantOwner
from apps.tenancy.viewsets import TenantScopedModelViewSet


class CajaViewSet(TenantScopedModelViewSet):
    """
    Where the business keeps money. Archived cajas are gone from here; they live on
    in the movements that name them. Nothing is ever deleted.
    """

    queryset = Caja.objects.filter(archived=False).prefetch_related('methods')
    serializer_class = CajaSerializer
    http_method_names = ('get', 'head', 'post', 'patch', 'put', 'options')
    pagination_class = None
    permission_classes = (*TenantScopedModelViewSet.permission_classes, HasCaja)

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.action not in ('list', 'retrieve'):
            permissions.append(IsTenantAdmin())
        return permissions

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant, opened_on=local_today(self.request.tenant))

    @extend_schema(request=None, responses={204: None})
    @action(detail=True, methods=['post'])
    def archive(self, request, pk=None):
        """Only an empty caja, with no medio de pago landing in it."""
        caja = self.get_object()
        if caja.methods.exists():
            raise Refused('A medio de pago still lands in this caja.', 'caja_in_use', status.HTTP_409_CONFLICT)
        if saldo(caja, local_today(request.tenant)) != 0:
            raise Refused('This caja still holds money.', 'caja_not_empty', status.HTTP_409_CONFLICT)
        caja.archived = True
        caja.save(update_fields=['archived', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=MethodMapSerializer, responses=CajaSerializer(many=True))
    @action(detail=False, methods=['put'], url_path='methods')
    def assign_methods(self, request):
        """Replace the whole map, so a medio de pago can never be left with no caja."""
        body = MethodMapSerializer(data=request.data, context=self.get_serializer_context())
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            MethodCaja.objects.for_tenant(request.tenant).delete()
            MethodCaja.objects.bulk_create([
                MethodCaja(tenant=request.tenant, payment_method=one['payment_method'], caja=one['caja'])
                for one in body.validated_data['assignments']
            ])
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class CajaSetupView(APIView):
    """
    Turning Caja on is counting the money: the one request carries every caja, what
    each holds and where each medio lands, so no half-set business shows a saldo.
    Owner only, like the other add-ons.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)
    # Dropping an add-on must work in the read-only window. Adding one is refused below.
    billing_exempt = True

    @extend_schema(responses=CajaNameSerializer(many=True))
    def get(self, request):
        """The cajas that have to be counted to turn Caja back on."""
        cajas = Caja.objects.for_tenant(request.tenant).filter(archived=False)
        return Response(CajaNameSerializer(cajas, many=True).data)

    @extend_schema(request=CajaSetupSerializer, responses={201: CajaSerializer(many=True)})
    def post(self, request):
        if pricing.billing_state(request.tenant, timezone.localdate()) == pricing.READ_ONLY:
            raise PermissionDenied('The payment period ended; the account is read-only.', code='billing_read_only')
        body = CajaSetupSerializer(data=request.data, context={'request': request})
        body.is_valid(raise_exception=True)
        today = local_today(request.tenant)
        with transaction.atomic():
            tenant = Tenant.objects.select_for_update().get(pk=request.tenant.pk)
            if tenant.addon_caja:
                raise Refused('Caja is already on.', 'caja_already_on', status.HTTP_409_CONFLICT)
            if not tenant.addon_cobros:
                raise Refused('Caja needs Cobros.', 'cobros_required', status.HTTP_409_CONFLICT)
            MethodCaja.objects.for_tenant(tenant).delete()
            for row in body.validated_data['cajas']:
                caja = self._counted(row, tenant, today)
                MethodCaja.objects.bulk_create([
                    MethodCaja(tenant=tenant, payment_method=method, caja=caja) for method in row['methods']
                ])
            tenant.addon_caja = True
            tenant.save(update_fields=['addon_caja', 'updated_at'])
        request.tenant.addon_caja = True
        cajas = Caja.objects.for_tenant(tenant).filter(archived=False).prefetch_related('methods')
        return Response(
            CajaSerializer(cajas, many=True, context={'request': request}).data, status=status.HTTP_201_CREATED
        )

    def _counted(self, row, tenant, today):
        caja = row.get('id')
        if caja is None:
            return Caja.objects.create(
                tenant=tenant, name=row['name'], opened_on=today, opening_balance=row['opening_balance']
            )
        # Today's count already holds today's movements, so they would add twice.
        if caja.entries.filter(occurred_on=today, voided_at__isnull=True).exists():
            raise Refused(
                'This caja moved money today, so it can only be counted again tomorrow.',
                'caja_moved_today', status.HTTP_409_CONFLICT,
            )
        caja.name, caja.opened_on, caja.opening_balance = row['name'], today, row['opening_balance']
        caja.save(update_fields=['name', 'opened_on', 'opening_balance', 'updated_at'])
        return caja

    @extend_schema(request=None, responses={204: None})
    def delete(self, request):
        """Turn Caja off. Its data stays; turning it back on counts the money again."""
        tenant = request.tenant
        tenant.addon_caja = False
        tenant.save(update_fields=['addon_caja', 'updated_at'])
        return Response(status=status.HTTP_204_NO_CONTENT)
