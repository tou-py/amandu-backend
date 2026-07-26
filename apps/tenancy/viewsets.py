from django.db.models import ProtectedError
from rest_framework import status, viewsets
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated

from apps.tenancy.permissions import HasActiveMembership


class Referenced(APIException):
    """
    409 rather than 400: nothing is wrong with the request. The row exists, the
    caller is allowed to delete it, and what stops them is the history hanging
    off it -- that is state, not input.
    """

    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This record is referenced by others and cannot be deleted.'
    default_code = 'referenced'


class TenantScopedModelViewSet(viewsets.ModelViewSet):
    """
    Base for every tenant-owned resource going through the tenant plane end to
    end: the token proves who the caller is, HasActiveMembership resolves which
    tenant they act for, and both the listing and every create are scoped to it.
    The tenant is set from the authenticated request, never from the payload.

    IsAuthenticated precedes HasActiveMembership so an anonymous caller gets 401,
    not 403.

    Subclasses set `queryset` and `serializer_class`. get_queryset re-scopes the
    class queryset per request via for_tenant, so `queryset` only names the model
    and its manager -- it must be a TenantOwnedMixin manager.

    This scopes the query ROOT only, which is necessary and not sufficient: a
    relation traversed from here reaches the related table unscoped (see
    TenantOwnedMixin). Validate cross-tenant FKs at the serializer.
    """

    permission_classes = (IsAuthenticated, HasActiveMembership)

    def get_queryset(self):
        return super().get_queryset().for_tenant(self.request.tenant)

    def perform_create(self, serializer):
        serializer.save(tenant=self.request.tenant)

    def perform_destroy(self, instance):
        """
        `on_delete=PROTECT` is what stops a booked client or a service in use
        from being erased out from under an appointment's history. Uncaught it
        reaches the handler as an unhandled ProtectedError -- a 500, which
        reports an operator's ordinary mistake as a server fault and tells them
        nothing about how to proceed.

        Handled here rather than per viewset because every tenant-owned resource
        routes through this base, and the two that are protected today (Client,
        Service) were both returning 500.
        """
        try:
            instance.delete()
        except ProtectedError:
            raise Referenced()
