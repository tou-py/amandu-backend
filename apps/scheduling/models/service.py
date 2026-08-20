from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class Service(TenantOwnedMixin, TimestampMixin):
    """
    Something a tenant offers and books time for. The
    duration is what turns an appointment's start into its end.
    """

    name = models.CharField(max_length=120)
    # What this service costs when it is charged per session, in whole guaraníes.
    # PYG has no fractional unit, so the integer IS the exact amount and a
    # DecimalField would only add a scale every reader has to remember to ignore
    # -- the same reasoning as CashEntry.amount, and do NOT "fix" it into one.
    #
    # NULL is the load-bearing state and the only signal for it: this service is
    # not charged per session. A pilates studio prices nothing here and sells its
    # clients a monthly plan instead (Client.monthly_fee); a salon prices every
    # service and sells nobody a plan. The shape of the data is the whole
    # difference -- there is no mode flag on the tenant to branch on, on purpose.
    price = models.PositiveBigIntegerField(null=True, blank=True)
    # A timedelta. DurationField stores it as a Postgres interval, so end =
    # start + duration is arithmetic the database does, not hand-rolled minutes.
    duration = models.DurationField()
    category = models.ForeignKey(
        'scheduling.Category',
        # Deleting a category must not delete its services -- the service still
        # exists, it just loses its label.
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='services',
    )

    class Meta:
        db_table = 'tb_service'
        ordering = ('name',)
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'name'],
                name='unique_service_name_per_tenant',
            ),
        ]

    @property
    def duration_minutes(self):
        """
        The duration as a whole number, for anything that shows it to a person.

        A template cannot divide, and a DurationField renders as '0:30:00',
        which is a machine talking. One definition so the API and the public
        page cannot disagree about what a thirty-minute haircut is.
        """
        return int(self.duration.total_seconds() // 60)

    def __str__(self):
        return self.name
