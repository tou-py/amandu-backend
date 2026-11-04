from datetime import date

from django.db.models import Count, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework import status
from rest_framework.exceptions import MethodNotAllowed, ValidationError
from rest_framework.response import Response

from apps.accounting.models import CashEntry
from apps.accounting.serializers import CashBookSummarySerializer, CashEntrySerializer, VoidSerializer
from apps.commons.errors import Refused
from apps.scheduling.billing import local_today
from apps.tenancy.permissions import HasCobros, IsTenantAdmin, IsTenantCoordinator
from apps.tenancy.viewsets import TenantScopedModelViewSet


BOOK_FILTERS = [
    OpenApiParameter('from', OpenApiTypes.DATE, description='Business day, YYYY-MM-DD, inclusive.'),
    OpenApiParameter('to', OpenApiTypes.DATE, description='Business day, YYYY-MM-DD, inclusive.'),
    OpenApiParameter('payment_method', str, enum=CashEntry.PaymentMethod.values),
]


def _day(params, field):
    value = params.get(field)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError({field: 'Use YYYY-MM-DD.'})


@extend_schema_view(
    list=extend_schema(parameters=BOOK_FILTERS),
    retrieve=extend_schema(exclude=True),
)
class CashEntryViewSet(TenantScopedModelViewSet):
    """
    The daily cash book: what came in, read back as the Cobros screen's
    Movimientos, plus filing one movement when a turn is closed and paid.

    A docstring of its own and not decoration: drf-spectacular describes an
    endpoint with `inspect.getdoc(view)`, which walks the MRO, so without one
    here the public documentation would describe the base class's internals.
    """

    queryset = CashEntry.objects.all()
    serializer_class = CashEntrySerializer
    # Never edited or deleted: the book is corrected by voiding.
    http_method_names = ('get', 'head', 'post', 'options')
    # Filing a movement is an act of the day, so the front desk may do it, not
    # only the people who own the shop's figures, and it reads what it filed.
    # The cash book is part of Cobros, so HasCobros goes before the role check.
    permission_classes = (*TenantScopedModelViewSet.permission_classes, HasCobros, IsTenantCoordinator)

    def get_queryset(self):
        return super().get_queryset().select_related('client', 'recorded_by')

    def _book(self):
        """The list and its totals answer the same filters, so they cannot disagree."""
        params = self.request.query_params
        entries = self.get_queryset().filter(kind=CashEntry.Kind.INCOME)
        since, until = _day(params, 'from'), _day(params, 'to')
        if since and until and until < since:
            raise ValidationError({'to': 'Cannot be before `from`.'})
        if since:
            entries = entries.filter(occurred_on__gte=since)
        if until:
            entries = entries.filter(occurred_on__lte=until)
        method = params.get('payment_method')
        if method:
            if method not in CashEntry.PaymentMethod.values:
                raise ValidationError({'payment_method': 'Unknown payment method.'})
            entries = entries.filter(payment_method=method)
        return entries

    def list(self, request, *args, **kwargs):
        """The book, most recent first, voided entries included and marked."""
        page = self.paginate_queryset(self._book())
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def retrieve(self, request, *args, **kwargs):
        # GET is here for the list: nothing reads one entry alone.
        raise MethodNotAllowed('GET')

    @extend_schema(parameters=BOOK_FILTERS, responses=CashBookSummarySerializer)
    @action(detail=False, methods=['get'], pagination_class=None)
    def summary(self, request):
        """
        What came in for these filters, whole and per method. Voided entries count
        for nothing, the same rule debt and cover follow.
        """
        live = self._book().filter(voided_at__isnull=True).order_by()
        rows = live.values('payment_method').annotate(total=Sum('amount'), count=Count('id')).order_by('-total')
        by_method = list(rows)
        return Response({
            'total': sum(row['total'] for row in by_method),
            'count': sum(row['count'] for row in by_method),
            'by_method': by_method,
        })

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant, recorded_by=self.request.user)

    def get_permissions(self):
        permissions = super().get_permissions()
        if self.action == 'void':
            # Taking money is easy and undoing it is controlled: anyone may
            # charge, only owner/admin may reverse a charge. Ahead of the
            # coordinator check, so staff are told `admin_required` too.
            permissions.insert(-1, IsTenantAdmin())
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
            raise Refused('This payment is already void.', 'already_voided', status.HTTP_409_CONFLICT)
        entry.voided_at = timezone.now()
        entry.voided_on = local_today(request.tenant)
        entry.voided_by = request.membership
        entry.void_reason = body.validated_data['reason']
        entry.save(update_fields=['voided_at', 'voided_on', 'voided_by', 'void_reason', 'updated_at'])
        return Response(self.get_serializer(entry).data)
