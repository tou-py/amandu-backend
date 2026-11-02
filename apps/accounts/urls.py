from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenBlacklistView, TokenRefreshView

from apps.accounts.views import (
    AcceptInvitationView,
    ChangePasswordView,
    InvitationViewSet,
    LoginView,
    MemberViewSet,
    MeView,
    NotificationViewSet,
    PasswordResetConfirmView,
    PasswordResetView,
    PushSubscriptionView,
    ResendVerificationView,
    SignupView,
    VerifyEmailView,
)

app_name = 'accounts'

router = DefaultRouter()
router.register('invitations', InvitationViewSet, basename='invitation')
router.register('notifications', NotificationViewSet, basename='notification')
router.register('members', MemberViewSet, basename='member')

urlpatterns = [
    path('signup/', SignupView.as_view(), name='signup'),
    path('verify-email/', VerifyEmailView.as_view(), name='verify-email'),
    path('verify-email/resend/', ResendVerificationView.as_view(), name='verify-email-resend'),
    path('password-reset/', PasswordResetView.as_view(), name='password-reset'),
    path('password-reset/confirm/', PasswordResetConfirmView.as_view(), name='password-reset-confirm'),
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    # POST {refresh}: blacklists it, so the session cannot be refreshed again.
    # The access token still lives out its 15 minutes; the client drops it.
    path('logout/', TokenBlacklistView.as_view(), name='logout'),
    path('me/', MeView.as_view(), name='me'),
    path('me/password/', ChangePasswordView.as_view(), name='change-password'),
    path('push/subscriptions/', PushSubscriptionView.as_view(), name='push-subscription'),
    # Before the router: 'accept' must not be captured as an invitation pk.
    path('invitations/accept/', AcceptInvitationView.as_view(), name='invitation-accept'),
] + router.urls
