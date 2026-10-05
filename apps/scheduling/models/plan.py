from django.core.validators import MinValueValidator
from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class Plan(TenantOwnedMixin, TimestampMixin):
    """
    One line of the business's catalogue of monthly plans: "Pilates 8 clases",
    "Libre". Set once per plan, so a price change is one edit instead of one per
    client file, and two clients on the same plan cannot silently pay different
    amounts -- the special case is Subscription.price_override, never a second
    copy of the plan.

    Archived rather than deleted when nobody should join any more: the
    subscriptions already on it keep running, and the periods they paid keep
    pointing at the plan that priced them.
    """

    name = models.CharField(max_length=120)
    # What one period costs, in whole guaraníes. Same reasoning as
    # CashEntry.amount: PYG has no fractional unit, so the integer IS the exact
    # amount -- do NOT "fix" it into a DecimalField.
    #
    # Read as "the price from the next unpaid period on". A paid period carries
    # the amount it was paid at on its cash entry, so editing this never
    # rewrites what anybody already paid.
    price = models.PositiveBigIntegerField()
    # How many turns one period includes. NULL is the load-bearing state:
    # unlimited. Zero would be a plan that covers nothing, which is not a plan.
    sessions_per_period = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1)]
    )
    # Which services the plan covers, by category. EMPTY MEANS ALL, so the
    # common case -- a studio with one plan for everything -- needs no setup.
    # Read that before writing a check: `categories.exists()` false is the
    # widest plan, not the narrowest.
    categories = models.ManyToManyField('scheduling.Category', blank=True, related_name='plans')
    archived = models.BooleanField(default=False)

    class Meta:
        db_table = 'tb_plan'
        ordering = ('name',)
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'name'], name='unique_plan_name_per_tenant'),
        ]

    def __str__(self):
        return self.name


class Subscription(TenantOwnedMixin, TimestampMixin):
    """
    A client on a plan, from a start date to (maybe) an end date.

    The start date's day of the month IS the anchor every period is counted
    from (see apps/scheduling/billing.py), unless `anchor_day` says otherwise.
    A subscription is never edited into another plan: changing plan ends this
    row after its current (or last paid) period and opens a new one the next
    day, so the periods already owed or paid keep the price they had.

    Nothing here says whether the client is up to date. Coverage and debt are
    computed from this row and the cash book every time they are asked, never
    stored: a stored "expired" flag needs a sweep to keep it true and is wrong
    between ticks.
    """

    client = models.ForeignKey(
        'scheduling.Client',
        # The subscription is part of the client file and goes with it. The
        # money does not: CashEntry.subscription is SET_NULL, so the book keeps
        # every payment after the arrangement it paid for is gone.
        on_delete=models.CASCADE,
        related_name='subscriptions',
    )
    plan = models.ForeignKey(
        'scheduling.Plan',
        # A plan in use is archived, not deleted; deleting it would take away
        # the price every one of these periods is charged at.
        on_delete=models.PROTECT,
        related_name='subscriptions',
    )
    # The family discount or the old price honoured for one client. NULL means
    # "whatever the plan costs now", which is what follows a plan's price change.
    price_override = models.PositiveBigIntegerField(null=True, blank=True)
    start_date = models.DateField()
    # The LAST DAY covered, inclusive. NULL is open: the plan runs until somebody
    # ends it, which is a deliberate act, never a sweep.
    end_date = models.DateField(null=True, blank=True)
    # Only set by a plan change: a successor that opens on a clamped day (Feb
    # 28th for an anchor of 31) keeps the original anchor. NULL is start_date.day.
    anchor_day = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table = 'tb_subscription'
        ordering = ('-start_date',)
        constraints = [
            # At most one running subscription per client, so coverage is never
            # ambiguous. Overlapping CLOSED ranges cannot be expressed as a unique
            # constraint, so SubscriptionSerializer rejects those; this is the
            # backstop for the common race, two open ones at once.
            models.UniqueConstraint(
                fields=['client'],
                condition=models.Q(end_date__isnull=True),
                name='one_open_subscription_per_client',
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F('start_date')),
                name='subscription_ends_after_it_starts',
            ),
        ]

    @property
    def price(self):
        """What one period costs right now: the override, else the plan's price."""
        return self.plan.price if self.price_override is None else self.price_override

    def __str__(self):
        return f'{self.client} · {self.plan}'
