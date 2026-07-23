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
        SCHEDULED = 'scheduled', 'Scheduled'
        COMPLETED = 'completed', 'Completed'
        CANCELLED = 'cancelled', 'Cancelled'
        NO_SHOW = 'no_show', 'No show'

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
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'tb_appointment'
        ordering = ('start',)
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

    def _require_scheduled(self):
        if self.status != self.Status.SCHEDULED:
            raise ValidationError(
                f'A {self.get_status_display().lower()} appointment is terminal.'
            )

    def cancel(self, reason=''):
        self._require_scheduled()
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

    def mark_no_show(self):
        self._require_scheduled()
        self.status = self.Status.NO_SHOW
        self.save(update_fields=['status', 'updated_at'])


class AppointmentClient(models.Model):
    """Through row for Appointment.clients. Its only job is the PROTECT on the
    client side; deleting the appointment (CASCADE) takes its own links."""

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

    class Meta:
        db_table = 'tb_appointment_client'
        constraints = [
            models.UniqueConstraint(
                fields=['appointment', 'client'],
                name='unique_client_per_appointment',
            ),
        ]
