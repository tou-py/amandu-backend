from django.db import models
from phonenumber_field.modelfields import PhoneNumberField

from apps.tenancy.mixins import TenantOwnedMixin


class OutboundMessage(TenantOwnedMixin):
    """
    One WhatsApp message waiting to go out, or the record that it did.

    An outbox and not a send-on-the-spot, for the same reason Notification has
    `pushed_at`: the turno is written in a transaction and WhatsApp is a remote
    service that is down, slow or rate-limiting whenever it likes. Filing the
    message with the turn means neither can exist without the other; sending it
    is the sweep's job (send_reminders), paced so a shared number does not look
    like a spam bot (apps/scheduling/whatsapp.py).

    `body` is the finished text, rendered when the event happened: the message
    says what was true then, even if the turno moves before it is sent.
    """

    appointment = models.ForeignKey(
        'scheduling.Appointment',
        on_delete=models.SET_NULL,
        null=True,
        related_name='+',
    )
    to = PhoneNumberField()
    body = models.TextField()
    attempts = models.PositiveSmallIntegerField(default=0)
    # Null until WhatsApp accepted it. The whole idempotency mechanism, like
    # Notification.pushed_at.
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tb_outbound_message'
        ordering = ('created_at',)

    def __str__(self):
        return f'{self.to} ({"sent" if self.sent_at else "pending"})'
