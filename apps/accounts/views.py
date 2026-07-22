from rest_framework_simplejwt.views import TokenObtainPairView

from apps.accounts.serializers import TenantAwareTokenObtainPairSerializer


class LoginView(TokenObtainPairView):
    """Email/password login that also returns the user's active memberships."""

    serializer_class = TenantAwareTokenObtainPairSerializer
