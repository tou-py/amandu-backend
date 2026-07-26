from django.conf import settings
from django.db import models


class Membership(models.Model):
    """
    Join row between a user and a tenant, and the reason CustomUser has no
    `tenant` FK: the same person may work for several tenants with a different
    role in each, so access lives here, not on the user.

    A user with zero memberships is valid and deliberate: that is a
    platform-level account (a superuser), which belongs to no tenant.
    """

    class Role(models.TextChoices):
        OWNER = 'owner', 'Owner'
        ADMIN = 'admin', 'Admin'
        # Runs the diary without running the business: attends clients like
        # staff, and books for the whole team like an admin, but holds none of
        # an admin's authority over who belongs to the tenant. The senior hand
        # at the front desk who also works the floor -- a real job that neither
        # the roles above nor the one below describes.
        COORDINATOR = 'coordinator', 'Coordinator'
        STAFF = 'staff', 'Staff'

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        # Access to THIS tenant cut off without removing the row, so history and
        # role survive. Distinct from Tenant.Status: one suspends the whole
        # tenant, this suspends one person inside it.
        SUSPENDED = 'suspended', 'Suspended'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    tenant = models.ForeignKey(
        'tenancy.Tenant',
        on_delete=models.CASCADE,
        related_name='memberships',
    )
    role = models.CharField(
        max_length=20,
        choices=Role.choices,  # type: ignore
        default=Role.STAFF,
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,  # type: ignore
        default=Status.ACTIVE,
    )
    # Having access to a tenant and attending its clients are different facts: a
    # receptionist books appointments without ever being booked. Default False on
    # purpose -- appearing in the agenda is a deliberate act, not a side effect of
    # being given a login.
    attends_appointments = models.BooleanField(default=False)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tb_membership'
        constraints = [
            # One membership row per (user, tenant); the role is what varies,
            # never the pairing.
            models.UniqueConstraint(
                fields=['user', 'tenant'],
                name='unique_membership_per_user_tenant',
            ),
        ]

    @classmethod
    def professionals_for(cls, tenant):
        """
        Who may be booked in this tenant's agenda. One definition on purpose: the
        answer is needed both to validate an appointment's professional and to
        list them for the agenda, and two copies of the same filter drift apart.
        """
        return cls.objects.filter(
            tenant=tenant,
            status=cls.Status.ACTIVE,
            attends_appointments=True,
        )

    def can_schedule_for_others(self):
        """
        May this member book an appointment under someone else's name.

        One definition on purpose: the serializer enforces it and the front end
        mirrors it to decide whether the professional field is editable, and two
        copies of the same rule drift apart.

        Staff are excluded deliberately. Booking for a colleague fills THEIR day,
        which they are the one accountable for, so it takes a role that answers
        for the diary as a whole.
        """
        return self.role in (self.Role.OWNER, self.Role.ADMIN, self.Role.COORDINATOR)

    def display_name(self):
        """Label for the agenda. Falls back to the email, which always exists."""
        return self.user.get_full_name().strip() or self.user.email

    def __str__(self):
        return f'{self.user} @ {self.tenant} ({self.role})'
