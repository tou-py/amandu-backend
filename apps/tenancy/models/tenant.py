from django.db import models

from apps.commons.mixins import TimestampMixin


class Tenant(TimestampMixin):
    """
    An organization using the platform. Every domain row in the system belongs
    to exactly one. A user, in contrast, may belong to many (see accounts
    Membership): the same person can work for several tenants.

    `slug` is the only globally unique value in the project by design: it names
    the tenant itself. Everything else is unique *within* a tenant, which is why
    TenantOwnedMixin exists and why plain `unique=True` is a bug on any
    tenant-owned model.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        # Access cut off (unpaid, under review), but the data is intact and the
        # tenant can come back.
        SUSPENDED = 'suspended', 'Suspended'
        # Left the platform. Data retained for the contractual window, then
        # deleted for real.
        CLOSED = 'closed', 'Closed'

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=60, unique=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices, # type: ignore
        default=Status.ACTIVE,
    )

    class Meta:
        db_table = 'tb_tenant'
        ordering = ('name',)

    def __str__(self):
        return self.name

    @property
    def is_operational(self) -> bool:
        """Whether the tenant's users may work. Different from existing."""
        return self.status == self.Status.ACTIVE
