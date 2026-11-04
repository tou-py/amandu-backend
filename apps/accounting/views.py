from datetime import date

from django.db.models import Count, Q, Sum
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework import status
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.accounting.models import CashEntry
from apps.accounting.serializers import CashBookSummarySerializer, CashEntrySerializer, VoidSerializer
from apps.commons.errors import Refused
from apps.scheduling.billing import local_today
from apps.tenancy.permissions import HasCaja, HasCobros, IsTenantAdmin, IsTenantCoordinator
from apps.tenancy.viewsets import TenantScopedModelViewSet


BOOK_FILTERS = [
    OpenApiParameter('from', OpenApiTypes.DATE, description='Business day, YYYY-MM-DD, inclusive.'),
    OpenApiParameter('to', OpenApiTypes.DATE, description='Business day, YYYY-MM-DD, inclusive.'),
    OpenApiParameter('payment_method', str, enum=CashEntry.PaymentMethod.values),
    OpenApiParameter('caja', int),
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
    list=extend_schema(parameters=[
        *BOOK_FILTERS,
        OpenApiParameter(
            'kind', str, enum=('income', 'expense', 'all'),
            description='Income by default, so a client that predates salidas never reads one as a payment.',
        ),
    ]),
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
        return super().get_queryset().select_related('client', 'recorded_by', 'caja')

    def _is_admin(self):
        return IsTenantAdmin().has_permission(self.request, self)

    def _filtered(self):
        """The list and its totals answer the same filters, so they cannot disagree."""
        params = self.request.query_params
        entries = self.get_queryset()
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
        caja = params.get('caja')
        if caja:
            if not caja.isdigit():
                raise ValidationError({'caja': 'Unknown caja.'})
            entries = entries.filter(caja=caja)
        return entries

    def _book(self):
        """Coordinators read every entrada but only the salidas they recorded."""
        kind = self.request.query_params.get('kind', 'income')
        if kind not in ('income', 'expense', 'all'):
            raise ValidationError({'kind': 'Unknown kind.'})
        entries = self._filtered()
        if kind == 'income':
            return entries.filter(kind=CashEntry.Kind.INCOME)
        if not self.request.tenant.addon_caja:
            raise PermissionDenied(HasCaja.message, code=HasCaja.code)
        expenses = Q(kind=CashEntry.Kind.EXPENSE)
        if not self._is_admin():
            expenses &= Q(recorded_by=self.request.user)
        return entries.filter(expenses if kind == 'expense' else expenses | Q(kind=CashEntry.Kind.INCOME))

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
        What came and went for these filters. Voided entries count for nothing, the
        same rule debt and cover follow. A `kind` filter changes nothing here: both
        directions always come back, for the screen to show what it filters on.
        """
        live = self._filtered().filter(voided_at__isnull=True).order_by()
        rows = live.filter(kind=CashEntry.Kind.INCOME).values('payment_method')
        by_method = list(rows.annotate(total=Sum('amount'), count=Count('id')).order_by('-total'))
        total = sum(row['total'] for row in by_method)
        answer = {
            'total': total,
            'count': sum(row['count'] for row in by_method),
            'by_method': by_method,
            'expenses': None,
            'by_category': None,
            'difference': None,
        }
        if request.tenant.addon_caja and self._is_admin():
            rows = live.filter(kind=CashEntry.Kind.EXPENSE).values('category')
            by_category = list(rows.annotate(total=Sum('amount'), count=Count('id')).order_by('-total'))
            expenses = sum(row['total'] for row in by_category)
            answer.update(expenses=expenses, by_category=by_category, difference=total - expenses)
        return Response(answer)

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
        if entry.kind == CashEntry.Kind.EXPENSE and not request.tenant.addon_caja:
            raise PermissionDenied(HasCaja.message, code=HasCaja.code)
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
