from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, RegexValidator
from django.db import models
from phonenumber_field.modelfields import PhoneNumberField

from apps.commons.mixins import TimestampMixin
from apps.tenancy import pricing


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
    # Where a self-served trial ends. "On trial" is derived, paid_until still equal
    # to this, so registering a payment ends the trial with no flag to clear.
    trial_until = models.DateField(null=True, blank=True)
    # The business's own number. It is what every WhatsApp sent on its behalf
    # gives as the contact, and setting it is what turns those messages on:
    # a message nobody can answer is worse than none (apps/scheduling/notify.py).
    phone = PhoneNumberField(blank=True)
    # How the business words its answer to a public request, sent from the
    # answerer's own WhatsApp (the turno sheet's "Avisar al cliente" step).
    # Blank is "Kyo's default", which lives in the frontend only, so there is
    # one place that renders it. {placeholders} are filled there too.
    request_accepted_message = models.TextField(blank=True, max_length=600)
    request_rejected_message = models.TextField(blank=True, max_length=600)
    # The object key of its logo in the image bucket, never a URL, so moving the
    # public domain needs no data migration (apps/commons/images.py).
    logo_key = models.CharField(max_length=200, null=True, blank=True, unique=True)
    # What the panel measured of the logo when it uploaded it -- the API never
    # sees the bytes -- so the public page can draw it to its shape and on a
    # plate it shows against (LogoMetaSerializer). Null when there is no logo,
    # or one uploaded before this existed.
    logo_meta = models.JSONField(null=True, blank=True)

    class BrandIntensity(models.TextChoices):
        SUBTLE = 'sutil', 'Subtle'
        BOLD = 'marcada', 'Bold'

    # The one colour the owner picks for the public page; every other token is
    # derived from it (apps/tenancy/theme.py). Blank is Kyo's own palette.
    brand_color = models.CharField(
        max_length=7, blank=True, validators=[RegexValidator(r'^#[0-9a-f]{6}$', 'A lowercase #rrggbb colour.')],
    )
    brand_intensity = models.CharField(
        max_length=7,
        choices=BrandIntensity.choices,  # type: ignore
        default=BrandIntensity.SUBTLE,
    )

    # Where the business is and where it lives online, for the public page
    # (apps/tenancy/presence.py turns them into links). All optional; the
    # serializer is what normalises what the owner pastes.
    address = models.CharField(max_length=200, blank=True, default='')
    # The owner's own "Compartir" link, which pins the exact place; without it
    # the page searches Maps for the address instead.
    maps_url = models.URLField(max_length=300, blank=True, default='')
    # Bare handles, no @ and no URL: the page builds the link and the label.
    instagram = models.CharField(max_length=60, blank=True, default='')
    facebook = models.CharField(max_length=60, blank=True, default='')
    tiktok = models.CharField(max_length=60, blank=True, default='')
    website = models.URLField(max_length=200, blank=True, default='')

    class PlanPeriodStart(models.TextChoices):
        # "Everyone pays October by the 10th": a new subscription starts on the
        # next 1st.
        MONTH_START = 'month_start', 'Month start'
        # "Ana pays every 14th": a new subscription starts the day she joins.
        JOIN_DAY = 'join_day', 'Join day'

    # How a NEW subscription's start date is prefilled, and nothing more. The
    # start date of each subscription is its anchor; changing this setting never
    # rewrites one that already exists, which is what makes it safe to flip.
    plan_period_start = models.CharField(
        max_length=20,
        choices=PlanPeriodStart.choices,  # type: ignore
        default=PlanPeriodStart.MONTH_START,
    )
    # Days after a period starts before it counts as overdue. 9 under
    # month_start reproduces "paid by the 10th" -- the 1st plus nine days. Capped
    # at 28 so a period is always due before the next one starts, even in
    # February; past that, "overdue" and "next month" would overlap.
    plan_grace_days = models.PositiveSmallIntegerField(
        default=9, validators=[MaxValueValidator(28)]
    )
    # The last LOCAL date the plan digest was claimed for this tenant. Not a
    # sent-at: send_reminders claims the day with one conditional UPDATE ("set
    # to today where not already today") before sending, so overlapping runs
    # cannot both send, and a run that missed 05:00 still catches up. NULL is
    # "never claimed".
    plan_digest_date = models.DateField(null=True, blank=True, editable=False)

    # What this tenant pays Kyo for beyond the agenda itself. Flags and not a
    # plan table: the price is the same for everyone and lives in
    # apps/tenancy/pricing.py, so the only fact that varies per tenant is
    # whether they bought it. Switched by a human in the admin, like paid_until.
    #
    # Cobros: monthly plans, charges and Por cobrar. Off for a new tenant; the
    # migration that added it turned it on for every tenant already using it,
    # so a pilot does not wake up to its money screens gone.
    addon_cobros = models.BooleanField(default=False)
    # Reservas: the public booking page, which is what lets strangers see this
    # tenant's free slots. The add-on IS the page, there is no second switch. Off
    # for a new tenant on purpose: a feature that opened an internal diary to the
    # internet by being deployed would be a breach, not a release.
    addon_reservas = models.BooleanField(default=False)
    # Facturación electrónica. The flag is the purchase; whether the tenant has
    # also configured its provider is a separate question the invoicing app asks.
    addon_facturacion = models.BooleanField(default=False)
    # Caja: cajas, salidas and saldos. Needs Cobros, since a caja that misses the
    # charges is wrong. Turned on by POST /api/cajas/setup/, which counts the money.
    addon_caja = models.BooleanField(default=False)
    # Same claim pattern as plan_digest_date, for the daily "pay Kyo" notice:
    # the last LOCAL date it was claimed, so overlapping runs send it once.
    billing_notice_date = models.DateField(null=True, blank=True, editable=False)

    class Meta:
        db_table = 'tb_tenant'
        ordering = ('name',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(addon_caja=False) | models.Q(addon_cobros=True),
                name='tenant_caja_needs_cobros',
            ),
        ]

    def start_trial(self, today):
        self.paid_until = self.trial_until = today + timedelta(days=pricing.TRIAL_DAYS)
        self.addon_cobros = self.addon_reservas = True
        self.save(update_fields=['paid_until', 'trial_until', 'addon_cobros', 'addon_reservas', 'updated_at'])

    def __str__(self):
        return self.name

    @property
    def is_operational(self) -> bool:
        """Whether the tenant's users may work. Different from existing."""
        return self.status == self.Status.ACTIVE
