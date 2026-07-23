from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.emails import send_invitation_email
from apps.accounts.models import Invitation
from apps.accounts.serializers import (
    AcceptInvitationSerializer,
    InvitationSerializer,
    TenantAwareTokenObtainPairSerializer,
    active_memberships,
)
from apps.tenancy.permissions import HasActiveMembership, IsTenantAdmin
from apps.tenancy.viewsets import TenantScopedModelViewSet


class LoginView(TokenObtainPairView):
    """Email/password login that also returns the user's active memberships."""

    serializer_class = TenantAwareTokenObtainPairSerializer


class MeView(APIView):
    """
    Identity plus the tenants the caller may act for, callable any time in the
    session. This is what lets a multi-tenant client render (and refresh) its
    tenant switcher without logging in again -- the membership list otherwise
    only arrives at login and is lost on a token refresh or a page reload.

    Switching needs no endpoint of its own: the client just sends a different
    X-Tenant-ID on the next request, and HasActiveMembership re-validates it.
    Requires only authentication, never an active tenant -- picking one is the
    whole point.
    """

    permission_classes = (IsAuthenticated,)

    def get(self, request):
        return Response(
            {
                'id': request.user.id,
                'email': request.user.email,
                'memberships': active_memberships(request.user),
            }
        )


class InvitationViewSet(TenantScopedModelViewSet):
    """
    Tenant admins provision their own staff here, scoped to the active tenant,
    without ever touching Django admin (which is not tenant-scoped). List and
    revoke act on pending invitations only; revoke keeps the row as history.
    """

    queryset = Invitation.objects.all()
    serializer_class = InvitationSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantAdmin)
    # No update: an invitation is issued or revoked, never edited in place.
    http_method_names = ('get', 'post', 'delete', 'head', 'options')

    def get_queryset(self):
        return super().get_queryset().filter(status=Invitation.Status.PENDING)

    def perform_create(self, serializer):
        invitation = serializer.save(
            tenant=self.request.tenant, invited_by=self.request.membership
        )
        # Covers a re-invite too: create() returns the refreshed row, so its new
        # token gets mailed.
        send_invitation_email(invitation)

    def perform_destroy(self, invitation):
        invitation.status = Invitation.Status.REVOKED
        invitation.save(update_fields=['status', 'updated_at'])


class AcceptInvitationView(APIView):
    """Public: the token in the body is the credential, so no auth runs here."""

    authentication_classes = ()
    permission_classes = (AllowAny,)

    def post(self, request):
        serializer = AcceptInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)
