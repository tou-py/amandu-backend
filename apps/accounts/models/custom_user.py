from datetime import timedelta

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from phonenumber_field.modelfields import PhoneNumberField

from apps.accounts.managers import CustomUserManager
from apps.commons.mixins import TimestampMixin


# The version of Kyo's terms and privacy policy a user accepts today. Must match
# `version` in kyo-landing/src/legal.js.
TERMS_VERSION = 1


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

    # How far ahead this person wants to be reminded of an appointment they are
    # about to give. A duration, not a number of minutes, because that is what it
    # is: the sweep subtracts it from `start` directly in the database, with no
    # unit to agree on between Python, Postgres and the client.
    #
    # It has no "off" value. Whether reminders arrive at all is answered by
    # PushSubscription -- see the note there on why a second flag would only be a
    # copy of the browser's answer.
    reminder_lead = models.DurationField(default=timedelta(minutes=30))
    # Where this person hears about their turnos by WhatsApp. On the user, not the
    # membership: it is their phone, the same in every tenant they work for.
    # Blank is a defined state -- no WhatsApp, push only.
    phone = PhoneNumberField(blank=True)
    # Null only for someone who signed up and has not opened the link yet; login
    # refuses them. Every other way in (invitation, admin, seeds) vouches for the
    # address, so the default is "verified" and signup is the one that passes None.
    email_verified_at = models.DateTimeField(null=True, blank=True, default=timezone.now)
    # The evidence of accepting the terms: when, and which text. Null for whoever
    # came in before there were any.
    terms_accepted_at = models.DateTimeField(null=True, blank=True)
    terms_version = models.PositiveSmallIntegerField(null=True, blank=True)

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

    @property
    def holds_a_password(self):
        """A password this person is known to have chosen. An unverified signup's
        may be a stranger's who typed their address, so it does not count."""
        return self.has_usable_password() and self.email_verified_at is not None

    def accept_terms(self):
        self.terms_accepted_at = timezone.now()
        self.terms_version = TERMS_VERSION
        self.save(update_fields=['terms_accepted_at', 'terms_version', 'updated_at'])
