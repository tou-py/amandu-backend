from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import IsAuthenticated

from apps.tenancy.permissions import HasActiveMembership, IsTenantOwner
from apps.tenancy.serializers import TenantSerializer


class TenantView(RetrieveUpdateAPIView):
    """
    The business the caller is acting for, readable and editable by its owner.

    A singleton with no id in the path, like /api/auth/me/: the tenant is
    already selected by X-Tenant-ID and re-validated against the caller's own
    memberships on every request, so an id here could only ever disagree with
    that -- and every way it could disagree is a bug or an attempt.

    Owner only. An admin runs the diary and the team; the name, the timezone and
    the country are the business itself.
    """

    serializer_class = TenantSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantOwner)

    def get_object(self):
        return self.request.tenant
