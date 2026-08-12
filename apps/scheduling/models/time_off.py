from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class TimeOff(TenantOwnedMixin, TimestampMixin):
    """
    A stretch the weekly schedule claims is open but is not: a public holiday,
    a closure, a holiday, an afternoon at the doctor.

    One model for all of them on purpose. A holiday table, an exception table and
    an absence table would hold the same three columns and be subtracted from
    availability by the same line of code -- what differs between them is the
    reason, which is text.

    `professional` null means the whole tenant is shut. Named on the row rather
    than duplicated per professional so closing on Christmas stays one write, and
    so it keeps being true for whoever is hired in March.
    """

    professional = models.ForeignKey(
        'accounts.Membership',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='time_off',
    )
    # Instants, not dates: "away 15:00-17:00" and "closed all Tuesday" are the
    # same fact at different sizes, and only the second one fits in a date.
    start = models.DateTimeField()
    end = models.DateTimeField()
    reason = models.CharField(max_length=120, blank=True)

    class Meta:
        db_table = 'tb_time_off'
        ordering = ('start',)
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end__gt=models.F('start')),
                name='time_off_ends_after_it_starts',
            ),
        ]

    def __str__(self):
        who = self.professional.display_name() if self.professional else 'whole tenant'
        return f'{who}: {self.start} - {self.end}'
