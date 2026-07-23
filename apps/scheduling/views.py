from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from apps.scheduling.models import Client
from apps.scheduling.serializers import ClientSerializer
from apps.tenancy.permissions import HasActiveMembership


class ClientViewSet(viewsets.ModelViewSet):
    """
    First resource to go through the tenant plane end to end: the token proves who
    the caller is, HasActiveMembership resolves which tenant they are acting for,
    and every query is scoped to it.

    IsAuthenticated is listed even though HasActiveMembership also rejects anonymous
    callers, because the order decides the status code: without it an anonymous
    request would get 403 where it should get 401.

    ponytail: the scoping lives here rather than in a shared base view. There is one
    tenant-owned resource so far; extract a base class when the second one exists and
    the duplication is real instead of predicted.
    """

    serializer_class = ClientSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership)

    def get_queryset(self):
        # Scoping the query root, which is necessary and not sufficient -- see
        # TenantOwnedMixin. Nothing here traverses a relation yet.
        return Client.objects.for_tenant(self.request.tenant)

    def perform_create(self, serializer):
        # The only place the tenant is ever assigned: from the validated request,
        # never from the payload.
        serializer.save(tenant=self.request.tenant)
