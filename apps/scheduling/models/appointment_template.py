from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.contrib.postgres.fields import ArrayField
from django.db import IntegrityError, models, transaction

from apps.commons.mixins import TimestampMixin
from apps.scheduling.models.appointment import Appointment
from apps.tenancy.mixins import TenantOwnedMixin


class AppointmentTemplate(TenantOwnedMixin, TimestampMixin):
    """
    The recurring SHAPE of a booking ("Spin lunes 10am, same three clients"),
    not a booking itself. Nothing here is ever shown to a client or blocks a
    professional's time; only `generate_occurrences` turns it into real
    `Appointment` rows, and only when a human asks it to -- there is no cron
    anywhere in this feature.

    `clients` is a plain M2M, not an `AppointmentClient`-style through table
    with PROTECT: a template is live config, not booking history, so a client
    leaving the roster if ever hard-deleted is an acceptable, much simpler
    tradeoff than the one `Appointment.clients` makes.
    """

    class Status(models.TextChoices):
        ACTIVE = 'active', 'Active'
        PAUSED = 'paused', 'Paused'
        ENDED = 'ended', 'Ended'

    professional = models.ForeignKey(
        'accounts.Membership',
        on_delete=models.PROTECT,
        related_name='appointment_templates',
    )
    service = models.ForeignKey(
        'scheduling.Service',
        on_delete=models.PROTECT,
        related_name='appointment_templates',
    )
    clients = models.ManyToManyField(
        'scheduling.Client',
        related_name='appointment_templates',
    )
    name = models.CharField(max_length=120)
    # Recurrence is one of two shapes, never both at once:
    #   * `weekdays` set -> occurrences land on those weekdays (Monday=0), so a
    #     single weekly slot is just a list of length 1 and Mon/Wed/Fri is the
    #     same list with three entries -- one mechanism, not a "weekly" branch
    #     that duplicates it.
    #   * `weekdays` empty -> every `interval_days` days from `start_date`
    #     (1 = daily, 7 = "every N days" weekly-by-count instead of by weekday).
    # See `_occurs_on`, the one place that reads either.
    interval_days = models.PositiveSmallIntegerField(default=1)
    weekdays = ArrayField(
        models.PositiveSmallIntegerField(),
        blank=True,
        default=list,
    )
    # Local wall-clock, not a UTC offset: the tenant timezone is what turns this
    # into an actual instant, resolved fresh at generation time so a DST change
    # between now and then is never baked into a stored delta.
    start_time = models.TimeField()
    start_date = models.DateField()
    # Both null = never-ending until a human pauses or ends the template.
    end_date = models.DateField(null=True, blank=True)
    max_occurrences = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,  # type: ignore
        default=Status.ACTIVE,
    )
    # See AppointmentViewSet.complete: the only automatic trigger in this
    # feature, fired as a direct synchronous consequence of a human's own
    # "mark completed" click. Never a background process.
    auto_generate_on_complete = models.BooleanField(default=False)

    class Meta:
        db_table = 'tb_appointment_template'
        ordering = ('name',)

    def __str__(self):
        return self.name

    def _occurs_on(self, candidate_date):
        if self.weekdays:
            return candidate_date.weekday() in self.weekdays
        return (candidate_date - self.start_date).days % self.interval_days == 0

    def generate_occurrences(self, count=1):
        """
        Walk forward from the last `Appointment` this template already
        produced (or `start_date`, if none exist yet) and create up to `count`
        further occurrences, stopping early at `end_date`/`max_occurrences`.

        Each occurrence is its own `transaction.atomic()` block. The
        `no_overlap_per_professional` ExclusionConstraint on `Appointment` can
        legitimately reject one date (something else already booked that
        slot); without per-occurrence isolation, that single INSERT failure
        would poison the whole outer transaction and fail every other date in
        the batch too, not just the colliding one.

        Returns {'created': [Appointment, ...], 'skipped': [{'date', 'reason'}, ...]}.
        Never touches an already-created Appointment -- a later edit to the
        template only changes future generation.
        """
        tz = ZoneInfo(self.tenant.timezone)
        existing = Appointment.objects.filter(template=self)
        last = existing.order_by('-start').first()
        cursor = last.start.astimezone(tz).date() if last is not None else self.start_date - timedelta(days=1)
        occurrences_so_far = existing.count()

        created = []
        skipped = []
        attempted = 0
        # Bounds the walk against a malformed rule that never matches (e.g. an
        # out-of-range weekday) so this can never spin forever looking for
        # `count` dates that satisfy the recurrence rule.
        for _ in range(3660):
            if attempted >= count:
                break
            cursor += timedelta(days=1)
            if self.end_date is not None and cursor > self.end_date:
                break
            if self.max_occurrences is not None and occurrences_so_far >= self.max_occurrences:
                break
            if not self._occurs_on(cursor):
                continue
            # `count` bounds occurrence DATES computed, not successes: a date
            # that turns out to conflict still spends one of the `count`
            # attempts, it just lands in `skipped` instead of `created`.
            attempted += 1

            local_start = datetime.combine(cursor, self.start_time, tzinfo=tz)
            start = local_start.astimezone(dt_timezone.utc)
            end = start + self.service.duration

            try:
                with transaction.atomic():
                    appointment = Appointment.objects.create(
                        tenant=self.tenant,
                        professional=self.professional,
                        service=self.service,
                        start=start,
                        end=end,
                        template=self,
                    )
                    appointment.clients.set(self.clients.all())
            except IntegrityError:
                skipped.append({'date': cursor.isoformat(), 'reason': 'conflict'})
                continue

            created.append(appointment)
            occurrences_so_far += 1

        return {'created': created, 'skipped': skipped}
