from django.db.models import Max
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.generics import ListAPIView
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.emails import send_invitation_email
from apps.accounts.models import Invitation, Membership, Notification
from apps.accounts.serializers import (
    AcceptInvitationSerializer,
    ActiveMembershipSerializer,
    ChangePasswordSerializer,
    InvitationSerializer,
    MeSerializer,
    MemberSerializer,
    NotificationSerializer,
    PushSubscriptionSerializer,
    TenantAwareTokenObtainPairSerializer,
)
from apps.commons.mixins import LastModifiedListMixin
from apps.tenancy.permissions import HasActiveMembership, IsTenantAdmin
from apps.tenancy.viewsets import TenantScopedModelViewSet


@extend_schema(
    responses=inline_serializer(
        name='Login',
        fields={
            'access': serializers.CharField(),
            'refresh': serializers.CharField(),
            'memberships': ActiveMembershipSerializer(many=True),
        },
    )
)
class LoginView(TokenObtainPairView):
    """Email/password login that also returns the user's active memberships."""

    serializer_class = TenantAwareTokenObtainPairSerializer
    # Tighter than the global anon rate: this is the endpoint a credential
    # brute-force hammers. Overrides DEFAULT_THROTTLE_CLASSES for this view.
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'login'


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

    PATCH edits the caller's own profile, and only ever theirs: the serializer is
    bound to request.user, so there is no id in the payload to point elsewhere.
    """

    permission_classes = (IsAuthenticated,)

    @extend_schema(responses=MeSerializer)
    def get(self, request):
        return Response(MeSerializer(request.user).data)

    @extend_schema(request=MeSerializer, responses=MeSerializer)
    def patch(self, request):
        serializer = MeSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class ChangePasswordView(APIView):
    """
    Separate from the profile PATCH on purpose: this one needs the current
    password as proof, returns no body, and is the endpoint worth throttling.
    Folding it into the profile would put a credential check on every name edit.
    """

    permission_classes = (IsAuthenticated,)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'change-password'

    @extend_schema(
        request=ChangePasswordSerializer,
        responses={204: OpenApiResponse(description='Password changed; no body.')},
    )
    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class PushSubscriptionView(APIView):
    """
    A browser registering to receive push notifications.

    No tenant: a subscription belongs to a device and the person using it, who
    may work for several tenants and wants one reminder stream for all of them.

    ONE verb, and no unsubscribe endpoint, because there is nothing for it to do.
    A browser that turns reminders off calls PushSubscription.unsubscribe(), and
    the push service then answers 404 for that endpoint (RFC 8030) -- which
    `send_reminders` already has to handle, since a subscription can expire on
    its own at any time. A DELETE here would be a second way to reach the state
    the first one reaches anyway, on the next send.

    Not a ViewSet either: the endpoint is a secret the client already holds, so
    listing them back would only widen where it can leak.
    """

    permission_classes = (IsAuthenticated,)

    @extend_schema(
        request=PushSubscriptionSerializer,
        responses={204: OpenApiResponse(description='Subscription stored; no body.')},
    )
    def post(self, request):
        serializer = PushSubscriptionSerializer(
            data=request.data, context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


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
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = 'accept-invitation'

    @extend_schema(
        request=AcceptInvitationSerializer,
        responses={204: OpenApiResponse(description='Membership created; no body.')},
    )
    def post(self, request):
        serializer = AcceptInvitationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MemberListView(ListAPIView):
    """
    Everyone with access to the active tenant, whether or not they are bookable.

    Not /api/professionals/, which answers "who may be booked" and therefore
    hides the receptionist and any admin who does not attend. An owner looking
    at their team needs to see the people, not the diary.

    Ordered so the list reads as a hierarchy rather than by insertion: owner
    first, then admins, then coordinators, then staff, alphabetically within
    each. The ordering is done in Python off the role choices so it cannot drift
    from the enum the way a hand-written Case/When would.
    """

    serializer_class = MemberSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership, IsTenantAdmin)
    pagination_class = None

    def get_queryset(self):
        rank = {role: index for index, role in enumerate(Membership.Role.values)}
        members = (
            Membership.objects
            .select_related('user')
            .filter(tenant=self.request.tenant)
        )
        return sorted(members, key=lambda m: (rank.get(m.role, 99), m.display_name().lower()))


class NotificationViewSet(LastModifiedListMixin, mixins.ListModelMixin, viewsets.GenericViewSet):
    """
    A membership's own feed of things it needs to see. List only -- nothing is
    ever created through this API, only by a trigger elsewhere writing the row
    directly (see AppointmentViewSet.cancel).
    """

    serializer_class = NotificationSerializer
    permission_classes = (IsAuthenticated, HasActiveMembership)

    # Schema-only: drf-spectacular needs a class queryset to derive the pk
    # type for mark_read's path param, and get_queryset() below requires a
    # real request (membership). .none() so nothing leaks if get_queryset()
    # is ever bypassed; it never runs in normal request handling.
    queryset = Notification.objects.none()

    def get_queryset(self):
        return (
            Notification.objects
            .filter(recipient=self.request.membership)
            .select_related('actor__user', 'appointment')
        )

    # Notification has no `updated_at` (LastModifiedListMixin's default): a row
    # is only ever created, then later has `read_at` set once by mark_read/
    # mark_all_read. Either one is a real change to what this list looks like,
    # so Last-Modified has to be the newer of the two, not just `created_at`.
    def get_last_modified(self, queryset):
        latest = queryset.aggregate(created=Max('created_at'), read=Max('read_at'))
        candidates = [value for value in latest.values() if value is not None]
        return max(candidates) if candidates else timezone.now()

    @extend_schema(request=None, responses=NotificationSerializer)
    @action(detail=True, methods=['post'])
    def mark_read(self, request, pk=None):
        """Idempotent: read_at is set once and never overwritten by a later call."""
        notification = self.get_object()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=['read_at'])
        return Response(self.get_serializer(notification).data)

    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description='Marked; no body.')},
    )
    @action(detail=False, methods=['post'])
    def mark_all_read(self, request):
        self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response(status=status.HTTP_204_NO_CONTENT)
