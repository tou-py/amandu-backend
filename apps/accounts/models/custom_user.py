from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.managers import CustomUserManager
from apps.commons.mixins import TimestampMixin


class CustomUser(AbstractUser, TimestampMixin):
    """User authenticated by email address instead of a username."""

    # AbstractUser is abstract, so assigning None removes the inherited field
    # instead of shadowing it. date_joined goes because TimestampMixin.created_at
    # already records it; see CustomUserAdmin, which cannot reuse UserAdmin's
    # fieldsets because of this.
    username = None
    date_joined = None

    email = models.EmailField(_('email address'), unique=True)

    # N:M through Membership: a user works for many tenants, each tenant has many
    # users, and the role per tenant lives on the join row. No `tenant` FK here
    # on purpose — a superuser belongs to no tenant (zero memberships), and there
    # is no single "the user's tenant" to point at.
    tenants = models.ManyToManyField(
        'tenancy.Tenant',
        through='accounts.Membership',
        related_name='users',
        blank=True,
    )

    USERNAME_FIELD = 'email'
    # USERNAME_FIELD and password are always prompted for; listing either here
    # makes createsuperuser ask twice.
    REQUIRED_FIELDS = []

    objects = CustomUserManager()

    class Meta:
        db_table = 'tb_user'
        verbose_name = 'User'
        verbose_name_plural = 'Users'

    def __str__(self):
        return self.email
