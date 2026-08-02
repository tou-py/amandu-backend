from calendar import monthrange
from datetime import date, timedelta

from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin

# How many bookings one request may put on the books. A term of twice-weekly
# pilates is about 50; six-monthly dental controls over five years is 10. This is
# not a business rule, it is the ceiling that stops a typo in `until` from
# inserting ten thousand rows in one transaction.
MAX_OCCURRENCES = 200


class AppointmentSeries(TenantOwnedMixin, TimestampMixin):
    """
    The arrangement behind a set of appointments: "every Monday and Wednesday at
    seven", "a check-up every six months".

    It holds the RULE and nothing else. The appointments it generated are
    ordinary rows carrying a FK back here, and they are the truth -- this table
    is never consulted to answer what is booked. That is deliberate and it is
    what makes the roadmap's hard requirement free: a cancelled or moved
    occurrence cannot break the series, because there is nothing to break. It is
    a booking that changed, and the rest are untouched rows.

    Occurrences are materialised rather than computed on read for the same
    reason. Overlap is enforced by a Postgres ExclusionConstraint over real rows;
    a virtual occurrence would be invisible to it, and equally invisible to the
    reminder sweep, to attendance, and to every query that already exists. One
    kind of appointment, not two.

    The series is bounded on purpose: `until` is required, so generation is a
    finite job at create time and needs no cron to keep a window rolling. A
    genuinely endless arrangement is renewed by creating the next term, which is
    also how a business thinks about it. The rolling horizon earns its keep the
    day somebody asks to stop renewing.
    """

    class Frequency(models.TextChoices):
        WEEKLY = 'weekly', 'Weekly'
        MONTHLY = 'monthly', 'Monthly'

    frequency = models.CharField(
        max_length=20,
        choices=Frequency.choices,  # type: ignore
        default=Frequency.WEEKLY,
    )
    # Every `interval` weeks or months: 1 is weekly, 3 is the salon's colour
    # root, 6 (monthly) is the dental control.
    interval = models.PositiveSmallIntegerField(default=1)
    # Monday is 0, matching date.weekday(). Weekly only, and it is why one
    # arrangement is one row: "Mondays and Wednesdays" is a single decision the
    # receptionist made, so cancelling it forward has to reach both.
    weekdays = ArrayField(models.PositiveSmallIntegerField(), default=list, blank=True)
    # The last day that may hold an occurrence, in the tenant's own calendar.
    # Inclusive.
    until = models.DateField()

    class Meta:
        db_table = 'tb_appointment_series'
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.get_frequency_display()} until {self.until}'

    def occurrence_dates(self, first: date) -> list[date]:
        """
        Every local calendar day this rule lands on, starting at `first`.

        Dates and not instants: the wall-clock time is applied afterwards, in the
        tenant timezone, so a 07:00 class stays at 07:00 across a DST boundary
        instead of drifting to 06:00 or 08:00 for half the term. Doing the
        arithmetic on UTC instants is exactly how that breaks.
        """
        if self.frequency == self.Frequency.MONTHLY:
            return self._monthly(first)
        return self._weekly(first)

    def _weekly(self, first: date) -> list[date]:
        # No weekday listed means "the same day as the first one", which is what
        # somebody who picked a date and said "every week" meant.
        weekdays = sorted(set(self.weekdays)) or [first.weekday()]
        # Back to the Monday of the first occurrence's week, so `interval` counts
        # whole weeks and a Wednesday start does not make the second week begin
        # on a Wednesday.
        monday = first - timedelta(days=first.weekday())

        dates = []
        while monday <= self.until and len(dates) < MAX_OCCURRENCES:
            for weekday in weekdays:
                day = monday + timedelta(days=weekday)
                # The first week is partial: days before the start belong to a
                # week that already happened.
                if first <= day <= self.until and len(dates) < MAX_OCCURRENCES:
                    dates.append(day)
            monday += timedelta(weeks=self.interval)
        return dates

    def _monthly(self, first: date) -> list[date]:
        dates = []
        year, month, day = first.year, first.month, first.day

        while len(dates) < MAX_OCCURRENCES:
            # A month too short for the day is SKIPPED, not clamped. Someone
            # booked the 31st; the 28th of February is a different day, and
            # quietly moving the appointment there is a decision nobody made.
            if day <= monthrange(year, month)[1]:
                current = date(year, month, day)
                if current > self.until:
                    break
                dates.append(current)
            elif date(year, month, monthrange(year, month)[1]) > self.until:
                break

            month += self.interval
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1

        return dates
