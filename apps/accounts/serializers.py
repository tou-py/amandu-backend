from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import CustomUser, Invitation, Membership
from apps.tenancy.models import Tenant


def active_memberships(user):
    """
    The tenants a user may currently act for, shaped for the client's tenant
    switcher. Filtered to exactly what HasActiveMembership will accept per
    request -- the membership is ACTIVE and its tenant is not suspended -- so the
    switcher never offers an option that then 403s on every call.
    """
    return [
        {
            'tenant_id': m.tenant_id,
            'tenant_slug': m.tenant.slug,
            'tenant_name': m.tenant.name,
            'tenant_country': str(m.tenant.country),
            'role': m.role,
        }
        for m in user.memberships.select_related('tenant').filter(
            status=Membership.Status.ACTIVE,
            tenant__status=Tenant.Status.ACTIVE,
        )
    ]


class ActiveMembershipSerializer(serializers.Serializer):
    """Documents the active_memberships() dict shape for the login and /me
    responses. Read-only: the payload is built by hand, this only describes it."""

    tenant_id = serializers.IntegerField()
    tenant_slug = serializers.SlugField()
    tenant_name = serializers.CharField()
    tenant_country = serializers.CharField()
    role = serializers.CharField()


class MemberSerializer(serializers.ModelSerializer):
    """
    One person on the team, as the team screen needs them.

    Distinct from ProfessionalSerializer, which answers a different question:
    that one lists who may be BOOKED, so it excludes anybody who does not attend
    and carries no role. This lists who BELONGS, so an owner can see the
    receptionist and the admin who never appear in an agenda.

    Read-only for now. Changing someone's role or whether they attend is a
    separate action with its own consequences -- an owner demoting themselves
    would lock the business out of its own settings -- and it is not what a list
    is for.
    """

    name = serializers.CharField(source='display_name', read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Membership
        fields = ('id', 'name', 'email', 'role', 'status', 'attends_appointments', 'joined_at')
        read_only_fields = fields


class MeSerializer(serializers.ModelSerializer):
    """
    The signed-in user's own view of themselves, and the only writable one: a
    person edits their own profile here, never anyone else's, because the view
    always binds it to request.user.

    `email` stays read-only. It is the USERNAME_FIELD, the address every
    invitation was sent to, and the key `Invitation.accept` matches on -- moving
    it is an account migration with its own verification, not a profile edit.

    The name is not per tenant on purpose: `Membership.display_name` reads
    `get_full_name()`, so editing it here relabels this person in every agenda
    they appear in. That is the intent -- it is their name, not their job title.
    """

    memberships = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ('id', 'email', 'first_name', 'last_name', 'memberships')
        read_only_fields = ('id', 'email')

    @extend_schema_field(ActiveMembershipSerializer(many=True))
    def get_memberships(self, user):
        return active_memberships(user)


class ChangePasswordSerializer(serializers.Serializer):
    """
    The current password is demanded even though the caller already holds a
    valid token. An access token proves the session was started by the owner at
    some point; it does not prove the person typing right now IS the owner, and
    a stolen token must not be enough to take the account over for good.

    Note what this does NOT do: the API has no token revocation yet, so sessions
    already open elsewhere keep working with their existing refresh token until
    it expires. Changing a password is not yet 'sign out everywhere'.
    """

    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        if not self.context['request'].user.check_password(value):
            raise serializers.ValidationError('Current password is incorrect.')
        return value

    def validate_new_password(self, value):
        user = self.context['request'].user
        try:
            # The user is passed so the similarity validator can compare against
            # their own email and name, which is most of what it is for.
            validate_password(value, user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages))
        return value

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save(update_fields=['password', 'updated_at'])
        return user


class TenantAwareTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Standard email/password login, plus the caller's active memberships in the
    response body (NOT in the token). The client needs this to know which tenants
    it may act for and which X-Tenant-ID header to send; a single-tenant user's
    client just uses the only entry.
    """

    def validate(self, attrs):
        data = super().validate(attrs)  # authenticates and sets self.user
        data['memberships'] = active_memberships(self.user)
        return data


class InvitationSerializer(serializers.ModelSerializer):
    """
    Admin-facing. `token` is deliberately NOT exposed: it is a bearer credential
    and travels only to the invitee's inbox, so not even the inviting admin can
    read it back and accept on someone's behalf. `tenant` and `invited_by` are
    absent too -- the view sets them from the request.
    """

    class Meta:
        model = Invitation
        fields = ('id', 'email', 'role', 'status', 'expires_at', 'created_at')
        read_only_fields = ('id', 'status', 'expires_at', 'created_at')

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
            existing.role = validated_data.get('role', existing.role)
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
