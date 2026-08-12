from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.commons.mixins import PublicIdentifierMixin, TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class TstzRange(models.Func):
    """tstzrange(start, end) with the default '[)' bounds -- inclusive start,
    exclusive end -- so two back-to-back appointments do not count as overlapping."""

    function = 'TSTZRANGE'
    output_field = DateTimeRangeField()


class Appointment(PublicIdentifierMixin, TenantOwnedMixin, TimestampMixin):
    """
    A booked slot: one professional attends one client for one service in a time
    range. Overlap is forbidden per professional at the database level, so two
    concurrent bookings cannot both win a race.

    Reaches its tenant by four paths -- own, professional, client, service -- and
    the database does not check they agree. The serializer scopes every FK to the
    request tenant so they always do.
    """

    class Status(models.TextChoices):
        """
        What happened to the BOOKING, not to the people in it. Whether a given
        client turned up is `AppointmentClient.attendance`: a slot can hold a
        group, and one absence there does not describe the other three.
        """

        # Asked for by a stranger through the public page, not yet accepted by
        # the shop. Holds the slot -- the overlap constraint excludes only
        # 'cancelled', so a request cannot be quietly double-booked while it
        # waits -- but nobody has promised to be there yet.
        PENDING = 'pending', 'Pending confirmation'
        SCHEDULED = 'scheduled', 'Scheduled'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'

    class Source(models.TextChoices):
        """
        Who put this in the diary. Worth recording from the first public booking
        onwards: "the shop booked it" and "someone off the street asked for it"
        carry different trust, and the no-show statistics of §5.3 are meaningless
        if the two are counted together.
        """

        STAFF = 'staff', 'Staff'
        PUBLIC = 'public', 'Public page'

    professional = models.ForeignKey(
        'accounts.Membership',
        # A booked professional cannot be deleted out from under their history.
        on_delete=models.PROTECT,
        related_name='appointments',
    )
    clients = models.ManyToManyField(
        'scheduling.Client',
        # Explicit through only to keep the client side PROTECT: a client with
        # appointments cannot be hard-deleted, the guard the single FK gave before
        # a slot could hold a group.
        through='AppointmentClient',
        related_name='appointments',
    )
    service = models.ForeignKey(
        'scheduling.Service',
        on_delete=models.PROTECT,
        related_name='appointments',
    )
    # The arrangement this slot was generated from, if it was. Null is the
    # normal case: most appointments are booked one at a time.
    #
    # SET_NULL, not CASCADE: deleting the rule must never delete the bookings it
    # made. They happened. What is lost is only the knowledge that they were once
    # part of a series, which is why nothing in the domain reads this except the
    # two "and the following ones" actions.
    series = models.ForeignKey(
        'scheduling.AppointmentSeries',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointments',
    )
    # Stored UTC (USE_TZ); the tenant timezone governs how it is shown, not stored.
    start = models.DateTimeField()
    # Derived from start + service.duration at write time and stored, so a later
    # edit to the service duration never shifts appointments already on the books.
    end = models.DateTimeField()
    status = models.CharField(
        max_length=20,
        choices=Status.choices,  # type: ignore
        default=Status.SCHEDULED,
    )
    # Cancelling is not deleting: the row stays visible in the agenda, it just
    # carries when and why it was called off.
    cancelled_at = models.DateTimeField(null=True, blank=True, editable=False)
    cancellation_reason = models.TextField(blank=True)
    # How many people this slot can hold. On the appointment and not on the
    # Service because the ceiling is physical, not commercial: it is how many
    # reformers are on the floor, how many chairs the room has -- and one of them
    # being broken this Tuesday is a fact about Tuesday, not about the service.
    #
    # Enforced in AppointmentSerializer, not by a constraint: the roster lives in
    # a second table, and no CheckConstraint can count rows there.
    #
    # 5 because the first tenant is a pilates studio, which is the only real
    # answer available. It is per appointment and editable, so a tenant whose
    # room holds twelve sets twelve and never thinks about this default again.
    capacity = models.PositiveSmallIntegerField(default=5)
    notes = models.TextField(blank=True)
    # When the professional was pushed a reminder for this appointment. Null means
    # not yet, and that is the whole idempotency mechanism: the sweep can run as
    # often as it likes, and re-run after being down, without notifying twice.
    #
    # A column and not a table because an appointment has exactly one recipient --
    # its professional. A reminders table would earn its place the day a second
    # person is notified about the same slot.
    #
    # ponytail: never cleared. Moving an already-reminded appointment does not
    # re-notify. Clear it in the reschedule path if that turns out to matter.
    reminder_sent_at = models.DateTimeField(null=True, blank=True, editable=False)
    source = models.CharField(
        max_length=20,
        choices=Source.choices,  # type: ignore
        default=Source.STAFF,
    )
    # Which member of staff booked it. Null is not "unknown": it is the public
    # page, where there is no member of staff, and Source says so explicitly
    # rather than leaving the reader to infer it from a null.
    #
    # SET_NULL, not PROTECT: an appointment booked by someone who has since left
    # is still an appointment, and the diary must not hold their membership row
    # hostage.
    created_by = models.ForeignKey(
        'accounts.Membership',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointments_booked',
    )

    class Meta:
        db_table = 'tb_appointment'
        ordering = ('start',)
        indexes = [
            # The agenda filters a date range and the reminder sweep runs every few
            # minutes forever; both walk this column and nothing else indexes it.
            models.Index(fields=['start'], name='appointment_start_idx'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end__gt=models.F('start')),
                name='appointment_end_after_start',
            ),
            # No two live appointments for the same professional may share time.
            # 'cancelled' literal, not Status.CANCELLED: the nested class is not in
            # scope inside Meta at class-definition time.
            ExclusionConstraint(
                name='no_overlap_per_professional',
                expressions=[
                    (TstzRange(models.F('start'), models.F('end')), RangeOperators.OVERLAPS),
                    ('professional', RangeOperators.EQUAL),
                ],
                condition=~models.Q(status='cancelled'),
            ),
        ]

    def __str__(self):
        return f'{self.professional} at {self.start:%Y-%m-%d %H:%M}'

    def _require_not_terminal(self):
        if self.status in (self.Status.COMPLETED, self.Status.CANCELLED):
            raise ValidationError(
                f'A {self.get_status_display().lower()} appointment is terminal.'
            )

    def _require_scheduled(self):
        self._require_not_terminal()
        if self.status != self.Status.SCHEDULED:
            raise ValidationError(
                'A booking still awaiting confirmation has not happened yet.'
            )

    def confirm(self):
        """
        The shop accepting a request that came off the public page. The only way
        into SCHEDULED other than being booked there directly.
        """
        if self.status != self.Status.PENDING:
            raise ValidationError('Only a pending appointment can be confirmed.')
        self.status = self.Status.SCHEDULED
        self.save(update_fields=['status', 'updated_at'])

    def cancel(self, reason=''):
        # Turning down a request off the public page and cancelling a confirmed
        # booking are the same transition: the slot is given back either way.
        # Only the two terminal states are refused.
        self._require_not_terminal()
        self.status = self.Status.CANCELLED
        self.cancelled_at = timezone.now()
        self.cancellation_reason = reason
        self.save(update_fields=['status', 'cancelled_at', 'cancellation_reason', 'updated_at'])

    def complete(self):
        self._require_scheduled()
        if self.end > timezone.now():
            raise ValidationError('Cannot complete an appointment that has not happened yet.')
        self.status = self.Status.COMPLETED
        self.save(update_fields=['status', 'updated_at'])


class AppointmentClient(models.Model):
    """
    Through row for Appointment.clients. Keeps the PROTECT on the client side --
    deleting the appointment (CASCADE) takes its own links -- and carries whether
    that particular person turned up.
    """

    class Attendance(models.TextChoices):
        # Nobody has said anything yet. The honest state for an appointment that
        # has not happened, and the one a fresh booking starts in.
        PENDING = 'pending', 'Pending'
        ATTENDED = 'attended', 'Attended'
        NO_SHOW = 'no_show', 'No show'
        # Told us they were not coming, too late to give the slot away. Worth
        # separating from a silent absence: the same outcome, different courtesy,
        # and a business that charges for one may not charge for the other.
        LATE_CANCEL = 'late_cancel', 'Late cancellation'

    appointment = models.ForeignKey(
        'scheduling.Appointment',
        on_delete=models.CASCADE,
        related_name='client_links',
    )
    client = models.ForeignKey(
        'scheduling.Client',
        on_delete=models.PROTECT,
        related_name='appointment_links',
    )
    attendance = models.CharField(
        max_length=20,
        choices=Attendance.choices,  # type: ignore
        default=Attendance.PENDING,
    )

    def mark(self, attendance):
        """
        A cancelled booking never ran, so nobody in it attended or failed to:
        recording either would invent an event that did not happen.
        """
        if self.appointment.status == Appointment.Status.CANCELLED:
            raise ValidationError('A cancelled appointment has no attendance to record.')
        self.attendance = attendance
        self.save(update_fields=['attendance'])

    class Meta:
        db_table = 'tb_appointment_client'
        # `id` is assigned once, at INSERT, and never changes again -- unlike
        # an unordered SELECT, which owes no ordering guarantee at all, so
        # marking one person's attendance (an UPDATE touching only their row)
        # could visibly reshuffle the roster on the next read even though
        # nobody's position in the group changed. Ordering by `id` pins the
        # roster to whatever order it was created in, permanently.
        ordering = ('id',)
        constraints = [
            models.UniqueConstraint(
                fields=['appointment', 'client'],
                name='unique_client_per_appointment',
            ),
        ]
