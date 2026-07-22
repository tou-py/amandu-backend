from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import Membership


class TenantAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Standard email/password login, plus the caller's active memberships in the
    response body (NOT in the token). The client needs this to know which tenants
    it may act for and which X-Tenant-ID header to send; a single-tenant user's
    client just uses the only entry.

    Only ACTIVE memberships are listed: a suspended membership is not an option
    the client should offer, and would be rejected by the per-request check
    anyway.
    """

    def validate(self, attrs):
        data = super().validate(attrs)  # authenticates and sets self.user
        data['memberships'] = [
            {
                'tenant_id': m.tenant_id,
                'tenant_slug': m.tenant.slug,
                'tenant_name': m.tenant.name,
                'role': m.role,
            }
            for m in self.user.memberships.select_related('tenant').filter(
                status=Membership.Status.ACTIVE
            )
        ]
        return data
