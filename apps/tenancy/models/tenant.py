from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from apps.commons.mixins import TimestampMixin


def validate_timezone(value):
    """
    Django has no timezone field, and a bad value here is not a cosmetic bug: every
    time the tenant sees is rendered through it, so garbage breaks the whole agenda
    at display time instead of at write time.

    Building the ZoneInfo IS the check -- comparing against available_timezones()
    would scan the entire tz database on every validation to answer the same question.
    """
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationError(
            '%(value)s is not a known IANA time zone.', params={'value': value}
        )


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
    # Instants are stored in UTC (USE_TZ). This is the single zone every time inside
    # the tenant is interpreted and displayed in: "9:00" means 9:00 here, for
    # everyone, whatever timezone the browser happens to be in.
    timezone = models.CharField(
        max_length=63,
        default='UTC',
        validators=[validate_timezone],
    )
    # ISO 3166-1 alpha-2, used as the default region when parsing a phone number the
    # staff typed in local format. Blank is a defined behaviour, not a hole: without a
    # region, phone numbers must arrive already in international form.
    # Shape only -- whether the region actually exists is answered by the phone
    # parser, which fails loudly on an unknown one.
    country = models.CharField(
        max_length=2,
        blank=True,
        default='',
        validators=[RegexValidator(r'^[A-Z]{2}$', 'Use an ISO 3166-1 alpha-2 code.')],
    )
    # The last day this tenant has paid for, whatever grace we decided to give
    # included -- a transfer that lands two days late is a phone call, not a
    # second field. NULL is a defined state and the default one: "never
    # expires", which is what every tenant onboarded before billing existed is,
    # and what a courtesy account stays. Only a date in the past suspends, so a
    # NULL is never swept (SQL comparisons against NULL are never true).
    #
    # Payment itself is manual for now: money arrives by bank transfer and a
    # human confirms it in the admin. This field is the only thing the rest of
    # the system needs to know about that, which is why there is no plan table,
    # no price and no ledger yet.
    paid_until = models.DateField(null=True, blank=True)

    class Meta:
        db_table = 'tb_tenant'
        ordering = ('name',)

    def __str__(self):
        return self.name

    @property
    def is_operational(self) -> bool:
        """Whether the tenant's users may work. Different from existing."""
        return self.status == self.Status.ACTIVE
