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
)
from apps.commons.errors import Refused
from apps.scheduling.billing import local_today
from apps.tenancy import pricing
from apps.tenancy.models import Tenant
from apps.tenancy.permissions import HasActiveMembership, HasCaja, IsTenantAdmin, IsTenantOwner
from apps.tenancy.viewsets import TenantScopedModelViewSet


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
