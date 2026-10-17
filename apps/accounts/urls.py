from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenBlacklistView, TokenRefreshView

from apps.accounts.views import (
    AcceptInvitationView,
    ChangePasswordView,
    InvitationViewSet,
    LoginView,
    MemberListView,
    MeView,
    NotificationViewSet,
    PushSubscriptionView,
)

app_name = 'accounts'

router = DefaultRouter()
router.register('invitations', InvitationViewSet, basename='invitation')
router.register('notifications', NotificationViewSet, basename='notification')

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    # POST {refresh}: blacklists it, so the session cannot be refreshed again.
    # The access token still lives out its 15 minutes; the client drops it.
    path('logout/', TokenBlacklistView.as_view(), name='logout'),
    path('me/', MeView.as_view(), name='me'),
    path('me/password/', ChangePasswordView.as_view(), name='change-password'),
    path('members/', MemberListView.as_view(), name='member-list'),
    path('push/subscriptions/', PushSubscriptionView.as_view(), name='push-subscription'),
    # Before the router: 'accept' must not be captured as an invitation pk.
    path('invitations/accept/', AcceptInvitationView.as_view(), name='invitation-accept'),
] + router.urls
