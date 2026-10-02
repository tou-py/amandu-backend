from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounting.models import CashEntry
from apps.accounting.serializers import CashEntrySerializer, VoidSerializer
from apps.tenancy.permissions import IsTenantAdmin, IsTenantCoordinator
from apps.tenancy.viewsets import TenantScopedModelViewSet


class CashEntryViewSet(TenantScopedModelViewSet):
    """
    Files one movement in the daily cash book -- what came in or went out when
    a turn is closed and paid.

    A docstring of its own and not decoration: drf-spectacular describes an
    endpoint with `inspect.getdoc(view)`, which walks the MRO, so without one
    here the public documentation would describe the base class's internals.
    """

    queryset = CashEntry.objects.all()
    serializer_class = CashEntrySerializer
    # Write-only: the front end files entries and never reads the book back.
    http_method_names = ('post', 'options')
    # Filing a movement is an act of the day, so the front desk may do it, not
    # only the people who own the shop's figures.
    permission_classes = (*TenantScopedModelViewSet.permission_classes, IsTenantCoordinator)

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.action == 'void':
            # Taking money is easy and undoing it is controlled: anyone may
            # charge, only owner/admin may reverse a charge.
            permissions.append(IsTenantAdmin())
        return permissions

    @extend_schema(request=VoidSerializer, responses=CashEntrySerializer)
    @action(detail=True, methods=['post'])
    def void(self, request, pk=None):
        """
        Reverse a payment, with a reason. The entry stays in the book, marked;
        from now on it counts for nothing -- not as cover, not against debt, not
        in totals -- and the period or turn it paid can be charged again.
        """
        entry = self.get_object()
        body = VoidSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        if entry.voided_at is not None:
            raise ValidationError('This payment is already void.')
        entry.voided_at = timezone.now()
        entry.voided_by = request.membership
        entry.void_reason = body.validated_data['reason']
        entry.save(update_fields=['voided_at', 'voided_by', 'void_reason', 'updated_at'])
        return Response(self.get_serializer(entry).data)
