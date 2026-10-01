from apps.accounting.models import CashEntry
from apps.accounting.serializers import CashEntrySerializer
from apps.tenancy.permissions import IsTenantCoordinator
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
