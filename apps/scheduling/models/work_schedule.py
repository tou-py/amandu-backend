from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class WorkSchedule(TenantOwnedMixin, TimestampMixin):
    """
    When a professional normally works: one row per continuous stretch of one
    weekday. The recurring shape of the week, not a calendar.

    Two rows for the same weekday are the normal case, not an edge one -- a shop
    that opens 09:00-12:00 and 14:00-19:00 closes for lunch, and a single
    start/end pair per day cannot say that. Modelling the stretch instead of the
    day gets the break for free.

    This is what an appointment's ExclusionConstraint cannot give: the constraint
    knows what is already TAKEN, never what is OPEN. An operator fills that gap
    from memory; a client booking from their phone cannot, so the answer has to
    live in a table.
    """

    professional = models.ForeignKey(
        'accounts.Membership',
        # The schedule describes the person. Without them it means nothing, and
        # unlike an appointment it is not history worth protecting.
        on_delete=models.CASCADE,
        related_name='work_schedules',
    )
    # Monday=0 .. Sunday=6, matching `datetime.date.weekday()`.
    #
    # Deliberately NOT Django's `__week_day` lookup, which is Sunday=1 .. and off
    # by one from Python's. Both conventions are in reach here, they differ
    # silently, and a schedule that is one day out is a bug nobody sees until a
    # client books on a closed Monday. Convert at the query, never store the
    # other one.
    weekday = models.PositiveSmallIntegerField(
        choices=[
            (0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'),
            (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday'),
        ],
    )
    # Wall-clock time in the tenant's timezone, not UTC. A shop opens at 09:00
    # local and keeps opening at 09:00 local across a DST change; storing the
    # instant instead would drift the door by an hour twice a year.
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        db_table = 'tb_work_schedule'
        ordering = ('weekday', 'start_time')
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_time__gt=models.F('start_time')),
                name='work_schedule_ends_after_it_starts',
            ),
            # Stops the same stretch being saved twice. It does NOT stop two
            # overlapping stretches (09:00-13:00 and 12:00-18:00), and that is on
            # purpose: overlapping shifts for one person on one day are just a
            # clumsy way of writing their union, which is what the availability
            # calculation reads them as anyway. Nothing is corrupted, so no
            # constraint has to defend it.
            models.UniqueConstraint(
                fields=['professional', 'weekday', 'start_time'],
                name='unique_work_schedule_start_per_professional_weekday',
            ),
        ]

    def __str__(self):
        return f'{self.get_weekday_display()} {self.start_time}-{self.end_time}'
