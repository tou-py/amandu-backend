from django.db import models


class Notification(models.Model):
    """
    One event a membership needs to see: something happened to a slot they own
    that they did not do themselves.

    `verb` is a choices field, not a boolean, for the same reason Appointment
    tracks `status` that way: "cancelled" is not the only thing worth telling
    someone about, and a boolean answers exactly one question forever. A second
    trigger (rescheduled, completed, ...) is a new choice and a new call site,
    never a schema change. Only one member exists today because only one
    trigger is wired -- the shape is ready, the rest is deliberately unbuilt.

    `recipient` and `actor` are both `Membership`, not `CustomUser`, for the
    same reason the rest of this app draws that line: identity here is
    tenant-scoped (Membership.__doc__), and a notification is about what
    happened in ONE tenant's agenda, not about the person in the abstract.

    `actor` is nullable and SET_NULL: the row is the recipient's record that
    something happened, and it must survive the acting membership being
    removed later. `appointment` is SET_NULL for the same reason -- the
    notification still reads (minus the vanished link) after the booking
    itself is gone.
    """

    class Verb(models.TextChoices):
        APPOINTMENT_CANCELLED = 'appointment_cancelled', 'Appointment cancelled'
        # A stranger asked for a slot through the public booking page. The one
        # verb with no actor: nobody inside the tenant did this.
        #
        # Worth pushing rather than leaving to the agenda's 30s poll, because a
        # request holds the slot while it waits. Ignored overnight it is not a
        # missed message, it is an hour of the diary nobody can sell and a
        # person who never got an answer.
        APPOINTMENT_REQUESTED = 'appointment_requested', 'Appointment requested'

    recipient = models.ForeignKey(
        'accounts.Membership',
        on_delete=models.CASCADE,
        related_name='notifications',
    )
    actor = models.ForeignKey(
        'accounts.Membership',
        on_delete=models.SET_NULL,
        null=True,
        related_name='+',
    )
    appointment = models.ForeignKey(
        'scheduling.Appointment',
        on_delete=models.SET_NULL,
        null=True,
        related_name='notifications',
    )
    verb = models.CharField(
        max_length=30,
        choices=Verb.choices,  # type: ignore
    )
    read_at = models.DateTimeField(null=True, blank=True)
    # When this row was pushed to the recipient's devices. Null means not yet,
    # and that is the whole idempotency mechanism -- the same one
    # Appointment.reminder_sent_at uses, for the same reason: the sweep can run
    # as often as it likes, and catch up after being down, without notifying
    # twice. Distinct from `read_at`, which is about the bell in the app; a
    # notification can be pushed and never read, or read in the app and never
    # pushed (nobody had a subscription at the time).
    pushed_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tb_notification'
        ordering = ('-created_at',)

    def __str__(self):
        return f'{self.get_verb_display()} -> {self.recipient}'
