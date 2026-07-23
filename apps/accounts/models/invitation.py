import secrets
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.accounts.models.custom_user import CustomUser
from apps.accounts.models.membership import Membership
from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin

INVITATION_TTL = timedelta(days=7)


def generate_token():
    return secrets.token_urlsafe(32)


def default_expiry():
    return timezone.now() + INVITATION_TTL


class Invitation(TenantOwnedMixin, TimestampMixin):
    """
    A pending grant of tenant access, separate from Membership on purpose: an
    invitation is an ephemeral provisioning artifact (token, expiry, revocable),
    a Membership is the durable access itself. The Membership is born only when
    the invitation is accepted, so a pending invite never counts as a member.

    The token is the credential: the accept endpoint is public and the token is
    what proves the holder was invited, exactly like a password-reset link.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'Pending'
        ACCEPTED = 'accepted', 'Accepted'
        REVOKED = 'revoked', 'Revoked'

    email = models.EmailField()
    role = models.CharField(
        max_length=20,
        choices=Membership.Role.choices,  # type: ignore
        default=Membership.Role.STAFF,
    )
    token = models.CharField(max_length=64, unique=True, default=generate_token, editable=False)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,  # type: ignore
        default=Status.PENDING,
    )
    expires_at = models.DateTimeField(default=default_expiry, editable=False)
    invited_by = models.ForeignKey(
        'accounts.Membership',
        # Keep the invitation's audit trail even if the inviter later leaves.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sent_invitations',
    )

    class Meta:
        db_table = 'tb_invitation'
        ordering = ('-created_at',)
        constraints = [
            # At most one live invitation per person per tenant; a re-invite
            # refreshes that row instead of stacking a second.
            models.UniqueConstraint(
                fields=['tenant', 'email'],
                condition=models.Q(status='pending'),
                name='unique_pending_invitation_per_tenant',
            ),
        ]

    def __str__(self):
        return f'{self.email} -> {self.tenant} ({self.status})'

    @property
    def is_acceptable(self):
        return self.status == self.Status.PENDING and self.expires_at > timezone.now()

    def refresh(self):
        """Re-issue a pending invitation with a new token and a new clock."""
        self.token = generate_token()
        self.expires_at = default_expiry()
        self.status = self.Status.PENDING
        self.save(update_fields=['role', 'token', 'expires_at', 'status', 'updated_at'])

    def accept(self, password):
        if not self.is_acceptable:
            raise ValidationError('This invitation is no longer valid.')

        user = CustomUser.objects.filter(email=self.email).first()
        if user is None:
            # Brand new person: the password they choose here is their first one.
            user = CustomUser.objects.create_user(email=self.email, password=password)
        elif not user.has_usable_password():
            # Invited before but never set a password; still theirs to set.
            user.set_password(password)
            user.save(update_fields=['password'])
        # An existing user with a usable password (works for another tenant) keeps
        # it: an invite token must never let anyone reset someone else's password.

        membership, _ = Membership.objects.get_or_create(
            user=user, tenant=self.tenant, defaults={'role': self.role}
        )
        self.status = self.Status.ACCEPTED
        self.save(update_fields=['status', 'updated_at'])
        return membership
