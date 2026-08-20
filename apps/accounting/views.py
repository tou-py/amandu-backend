from datetime import datetime

from django.db.models import Q, Sum
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.accounting.models import CashEntry
from apps.accounting.serializers import CashEntrySerializer, CashSummarySerializer
from apps.tenancy.permissions import IsTenantAdmin, IsTenantCoordinator
from apps.tenancy.viewsets import TenantScopedModelViewSet


RANGE_PARAMETERS = [
    OpenApiParameter(
        'from',
        OpenApiTypes.DATE,
        description='First business day to include (YYYY-MM-DD). Inclusive.',
    ),
    OpenApiParameter(
        'to',
        OpenApiTypes.DATE,
        description='Last business day to include (YYYY-MM-DD). Inclusive.',
    ),
]


@extend_schema_view(
    list=extend_schema(
        parameters=RANGE_PARAMETERS + [
            OpenApiParameter(
                'kind',
                OpenApiTypes.STR,
                enum=CashEntry.Kind.values,
                description='Only entries of this kind.',
            ),
        ],
    ),
)
class CashEntryViewSet(TenantScopedModelViewSet):
    """
    The daily cash book: what came in, what went out, and what the day was worth.

    A docstring of its own and not decoration: drf-spectacular describes an
    endpoint with `inspect.getdoc(view)`, which walks the MRO, so without one
    here the public documentation would describe the base class's internals.
    """

    queryset = CashEntry.objects.all()
    serializer_class = CashEntrySerializer

    # Two different questions, two different answers -- which is why the split
    # here is by action and not by SAFE_METHODS the way the schedule viewsets do
    # it. There the concern is who may CHANGE the shop's hours, so staff still
    # read them. Here it is inverted: filing one movement is an act of the day,
    # and reading the book back is the shop's figures.
    #
    # The widest gate sits at class level; get_permissions narrows everything
    # that is not a create.
    permission_classes = (*TenantScopedModelViewSet.permission_classes, IsTenantCoordinator)

    def get_permissions(self):
        """
        Filing an entry admits the coordinator; everything else stays owner and
        admin, READS included.

        `create` alone and not every write: correcting or deleting an entry
        means finding it first, and the front desk cannot list the book. Opening
        those too would grant an authority that can only be exercised by
        guessing an id.
        """
        permissions = super().get_permissions()
        if self.action != 'create':
            permissions.append(IsTenantAdmin())
        return permissions

    def get_queryset(self):
        """
        The book, filtered. `from`/`to` are business days (YYYY-MM-DD), both ends
        inclusive -- the same parameter shape the agenda uses, so one client
        helper builds the range for both.

        No timezone conversion here, unlike AppointmentViewSet: `occurred_on` is
        a DateField, a day the operator names rather than an instant the clock
        produced, so there is no local-vs-UTC day to reconcile.
        """
        queryset = super().get_queryset()
        params = self.request.query_params

        kind = params.get('kind')
        if kind is not None:
            if kind not in CashEntry.Kind.values:
                raise ValidationError({'kind': f'Must be one of {CashEntry.Kind.values}.'})
            queryset = queryset.filter(kind=kind)

        day_from = self._parse_day(params.get('from'), 'from')
        if day_from is not None:
            queryset = queryset.filter(occurred_on__gte=day_from)

        day_to = self._parse_day(params.get('to'), 'to')
        if day_to is not None:
            # __lte and not the agenda's "< next day": occurred_on is already a
            # date, so the whole 'to' day is one value, not a span of instants.
            queryset = queryset.filter(occurred_on__lte=day_to)

        return queryset

    @staticmethod
    def _parse_day(value, field):
        if value is None:
            return None
        try:
            return datetime.strptime(value, '%Y-%m-%d').date()
        except ValueError:
            raise ValidationError({field: 'Use YYYY-MM-DD.'})

    @extend_schema(parameters=RANGE_PARAMETERS, responses=CashSummarySerializer)
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """
        What the range came to: money in, money out, and the difference.

        The number the shop owner opens the app for, so it is one aggregate over
        the same filtered queryset the list uses -- adding rows up in Python
        would mean shipping a month of entries to compute three integers, and
        would silently disagree with the list the moment a filter changed.

        `default=0` rather than a null: an empty range is a real answer -- the
        day made nothing -- and a null there would force every caller to guess.
        """
        totals = self.filter_queryset(self.get_queryset()).aggregate(
            income=Sum('amount', filter=Q(kind=CashEntry.Kind.INCOME), default=0),
            expense=Sum('amount', filter=Q(kind=CashEntry.Kind.EXPENSE), default=0),
        )
        return Response({**totals, 'balance': totals['income'] - totals['expense']})
