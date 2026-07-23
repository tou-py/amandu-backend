from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import CustomUser, Invitation, Membership


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


class InvitationSerializer(serializers.ModelSerializer):
    """
    Admin-facing. `token` is returned on purpose: with no email delivery wired
    yet, the response IS how the admin gets the accept link to relay. `tenant`
    and `invited_by` are absent -- the view sets them from the request.
    """

    class Meta:
        model = Invitation
        fields = ('id', 'email', 'role', 'status', 'token', 'expires_at', 'created_at')
        read_only_fields = ('id', 'status', 'token', 'expires_at', 'created_at')

    def validate_email(self, value):
        tenant = self.context['request'].tenant
        if Membership.objects.filter(tenant=tenant, user__email=value).exists():
            raise serializers.ValidationError(
                'This person already has a membership in this tenant.'
            )
        return value

    def create(self, validated_data):
        # A re-invite refreshes the existing pending row (new token + clock)
        # instead of hitting the partial unique constraint with a second.
        existing = Invitation.objects.filter(
            tenant=validated_data['tenant'],
            email=validated_data['email'],
            status=Invitation.Status.PENDING,
        ).first()
        if existing is not None:
            existing.role = validated_data['role']
            existing.refresh()
            return existing
        return super().create(validated_data)


class AcceptInvitationSerializer(serializers.Serializer):
    """
    Public. The token is the credential. A password is required only when the
    invited email has no usable password yet (new user, or invited but never
    set one); an existing user keeps the password they already log in with.
    """

    token = serializers.CharField(write_only=True)
    password = serializers.CharField(write_only=True, required=False)

    def validate(self, attrs):
        invitation = Invitation.objects.filter(token=attrs['token']).first()
        if invitation is None or not invitation.is_acceptable:
            raise serializers.ValidationError('Invalid or expired invitation.')

        user = CustomUser.objects.filter(email=invitation.email).first()
        if user is None or not user.has_usable_password():
            password = attrs.get('password')
            if not password:
                raise serializers.ValidationError({'password': 'Set a password to accept.'})
            try:
                validate_password(password)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({'password': list(exc.messages)})

        attrs['invitation'] = invitation
        return attrs

    def save(self):
        return self.validated_data['invitation'].accept(self.validated_data.get('password'))
