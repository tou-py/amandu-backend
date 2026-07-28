from django.conf import settings
from django.db import models


class PushSubscription(models.Model):
    """
    One browser that agreed to receive push notifications for one user.

    Per BROWSER, not per user: the same person subscribes again on their phone,
    their laptop and every profile they use, and each of those is a separate row
    with its own endpoint. Deleting one does not silence the others.

    This row IS the opt-in. There is no "wants notifications" flag anywhere,
    because there is nothing to send to without a subscription and the browser
    already asked the question -- a second boolean would be a copy of that answer
    that drifts the moment someone revokes the permission in their browser. The
    unsubscribe path is the same: the row goes away.

    Not tenant-owned on purpose. A subscription belongs to a device, and the
    person holding it may work for several tenants; scoping it to one would mean
    subscribing again per tenant to be reminded of the same day's work.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='push_subscriptions',
    )
    # The push service's address for this browser (RFC 8030). Unique because the
    # browser hands back the same endpoint when it re-subscribes, so an upsert on
    # it is what keeps a returning device from accumulating duplicate rows.
    #
    # SECRET. MDN: "The endpoint URL therefore needs to be kept secret, or other
    # applications might be able to send push messages to your application." It
    # is never logged and never sent back to a client.
    endpoint = models.URLField(max_length=500, unique=True)
    # The two halves of the encryption material the browser generated. Without
    # them a message can be delivered but not decrypted, so they are as required
    # as the endpoint.
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'tb_push_subscription'
        ordering = ('-created_at',)

    def __str__(self):
        # Never the endpoint: this string reaches the admin and log lines.
        return f'{self.user} ({self.endpoint[:40]}...)'

    @property
    def subscription_info(self):
        """The shape pywebpush expects, which is also the shape the browser's
        PushSubscription.toJSON() produces -- reassembled from the columns."""
        return {
            'endpoint': self.endpoint,
            'keys': {'p256dh': self.p256dh, 'auth': self.auth},
        }
