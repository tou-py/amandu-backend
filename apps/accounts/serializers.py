from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from apps.accounts.models import CustomUser, Invitation, Membership, Notification, PushSubscription
from apps.scheduling.models import Appointment
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
            'membership_id': m.id,
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

    # The same id ProfessionalSerializer emits, so a client can tell which of the
    # bookable professionals is the person holding the session without matching
    # display names -- which is ambiguous the moment two people share one.
    membership_id = serializers.IntegerField()
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
    vapid_public_key = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = (
            'id',
            'email',
            'first_name',
            'last_name',
            'reminder_lead',
            'memberships',
            'vapid_public_key',
        )
        read_only_fields = ('id', 'email', 'vapid_public_key')

    @extend_schema_field(ActiveMembershipSerializer(many=True))
    def get_memberships(self, user):
        return active_memberships(user)

    @extend_schema_field(serializers.CharField)
    def get_vapid_public_key(self, user):
        """
        Not a fact about the user, and it rides here anyway.

        The browser has to pass this key to pushManager.subscribe() before any
        subscription exists, so the client needs it at boot -- and /me is the one
        request it already makes at boot. The alternative is baking it into the
        front-end build, which would make the key a second source of truth able
        to disagree silently with the private half this server signs with. Read
        from the server that holds the pair, it cannot drift.

        Public by definition -- RFC 8292 calls it "a stable identifier for the
        server" -- so returning it to an authenticated client exposes nothing.
        Empty when push is unconfigured, which the client reads as "do not offer
        reminders at all".
        """
        return settings.VAPID_PUBLIC_KEY


class PushSubscriptionSerializer(serializers.ModelSerializer):
    """
    What the browser's PushSubscription.toJSON() produces, flattened onto the
    model. The nested `keys` object is unpacked here rather than in the view, so
    the shape the client actually sends is what the schema documents.

    Write-only throughout: `endpoint` is a secret (MDN), and a client that just
    sent one has no use for it back. Hence no list endpoint and no response body.
    """

    # validators=[] strips the UniqueValidator ModelSerializer infers from the
    # model's unique=True. Without this, a browser re-subscribing -- which it does
    # routinely, after a pushsubscriptionchange or simply on the next visit -- is
    # answered 400 for sending the endpoint it was given, and create() below never
    # runs. Uniqueness is still enforced by the column; here it means "upsert".
    endpoint = serializers.URLField(max_length=500, validators=[])
    keys = serializers.DictField(child=serializers.CharField(), write_only=True)

    class Meta:
        model = PushSubscription
        fields = ('endpoint', 'keys')

    def validate_keys(self, value):
        missing = {'p256dh', 'auth'} - value.keys()
        if missing:
            raise serializers.ValidationError(
                f'Missing key material: {", ".join(sorted(missing))}.'
            )
        return value

    def create(self, validated_data):
        keys = validated_data.pop('keys')
        # Upsert, not create: a browser that re-subscribes hands back the same
        # endpoint, and a second row for it would push the same person the same
        # notification twice. The user is overwritten too -- on a shared device,
        # whoever logs in next must not keep notifying the previous account.
        subscription, _ = PushSubscription.objects.update_or_create(
            endpoint=validated_data['endpoint'],
            defaults={
                'user': self.context['request'].user,
                'p256dh': keys['p256dh'],
                'auth': keys['auth'],
            },
        )
        return subscription


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


class NotificationActorSerializer(serializers.ModelSerializer):
    """Same minimal {id, name} shape ProfessionalSerializer already uses for a
    membership -- a notification row needs no more than that to label who did it."""

    name = serializers.CharField(source='display_name', read_only=True)

    class Meta:
        model = Membership
        fields = ('id', 'name')


class NotificationAppointmentSerializer(serializers.ModelSerializer):
    """Just enough to point at the slot -- the agenda already holds the rest."""

    class Meta:
        model = Appointment
        fields = ('id', 'start')


class NotificationSerializer(serializers.ModelSerializer):
    """
    Read-only: notifications are never created through this API, only through
    the trigger that writes them server-side (AppointmentViewSet.cancel).
    """

    # allow_null on both: the model's actor/appointment FKs are SET_NULL, so a
    # row outlives the acting membership or the booking being deleted, and the
    # schema has to say so rather than claim a shape that then 500s never (DRF
    # itself renders None safely) but silently lies to every client about it.
    actor = NotificationActorSerializer(read_only=True, allow_null=True)
    appointment = NotificationAppointmentSerializer(read_only=True, allow_null=True)

    class Meta:
        model = Notification
        fields = ('id', 'verb', 'actor', 'appointment', 'read_at', 'created_at')
        read_only_fields = fields
