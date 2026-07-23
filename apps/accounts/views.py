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
)
from apps.tenancy.permissions import HasActiveMembership, IsTenantAdmin
from apps.tenancy.viewsets import TenantScopedModelViewSet


class LoginView(TokenObtainPairView):
    """Email/password login that also returns the user's active memberships."""

    serializer_class = TenantAwareTokenObtainPairSerializer


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
