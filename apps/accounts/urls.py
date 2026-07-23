from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import (
    AcceptInvitationView,
    InvitationViewSet,
    LoginView,
    MeView,
)

app_name = 'accounts'

router = DefaultRouter()
router.register('invitations', InvitationViewSet, basename='invitation')

urlpatterns = [
    path('login/', LoginView.as_view(), name='login'),
    path('refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('me/', MeView.as_view(), name='me'),
    # Before the router: 'accept' must not be captured as an invitation pk.
    path('invitations/accept/', AcceptInvitationView.as_view(), name='invitation-accept'),
] + router.urls
