from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class CashEntry(TenantOwnedMixin, TimestampMixin):
    """
    One movement of money in the shop's daily cash book: what came in, what went
    out, on which business day.

    Deliberately a book and not an accounting system. There is no double entry,
    no chart of accounts, no closing of periods and no reconciliation -- the
    question this table exists to answer is "how did today go", and the owner
    asks it from a phone between two clients.
    """

    class Kind(models.TextChoices):
        INCOME = 'income', 'Income'
        EXPENSE = 'expense', 'Expense'

    class PaymentMethod(models.TextChoices):
        CASH = 'cash', 'Cash'
        CARD = 'card', 'Card'
        TRANSFER = 'transfer', 'Transfer'
        OTHER = 'other', 'Other'

    kind = models.CharField(max_length=20, choices=Kind.choices)  # type: ignore
    # Whole guaraníes. PYG has no fractional unit -- no céntimos, no subdivision
    # of any kind -- so there is nothing for decimal places to hold and a Decimal
    # would only add a scale every reader has to remember to ignore. Do NOT
    # "fix" this into DecimalField: the integer IS the exact amount.
    #
    # Positive is not a claim that money only ever arrives. `kind` carries the
    # direction, so a negative amount here would be a second way of saying
    # "expense" that the summary aggregate would then double-count.
    #
    # Big rather than plain: a busy month in guaraníes runs into the hundreds of
    # millions, and a PositiveIntegerField tops out just above two billion.
    amount = models.PositiveBigIntegerField()
    # The business day the money moved, which is not the day the row was typed:
    # yesterday's takings get entered this morning, and the supplier invoice gets
    # entered whenever somebody finds it. `created_at` records the typing; this
    # records the event, and it is the only one the summary may be built on.
    occurred_on = models.DateField()
    concept = models.CharField(max_length=140)
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,  # type: ignore
        default=PaymentMethod.CASH,
    )
    # Optional, and optional in both directions: money moves for reasons that
    # were never booked (a walk-in, a supplier, the till float), and a booking
    # may never be paid at all.
    #
    # SET_NULL, never CASCADE: deleting an appointment must not delete the money
    # record. The money moved -- that is a fact about the shop's day, and it
    # stays true after whatever the slot was is gone. Losing the link costs the
    # attribution, not the entry.
    appointment = models.ForeignKey(
        'scheduling.Appointment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='cash_entries',
    )

    class Meta:
        db_table = 'tb_cash_entry'
        # Most recent business day first, id breaking the tie, so two entries on
        # the same day come back newest-typed first instead of in whatever order
        # the database felt like.
        ordering = ('-occurred_on', '-id')
        indexes = [
            # Every read of this table is "this tenant, this date range" -- the
            # list, the summary, the owner's month. The tenant column has to lead
            # for the index to serve that, since it is always an equality.
            models.Index(fields=['tenant', 'occurred_on'], name='cash_entry_tenant_day_idx'),
        ]
        verbose_name_plural = 'cash entries'

    def __str__(self):
        return f'{self.occurred_on} {self.kind} {self.amount}'
