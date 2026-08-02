from django.db import models
from phonenumber_field.modelfields import PhoneNumberField

from apps.commons.mixins import PublicIdentifierMixin, TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


class Client(PublicIdentifierMixin, TenantOwnedMixin, TimestampMixin):
    """
    Someone the tenant schedules appointments for. NOT a user of the system: a
    client has no password, no permissions and never logs in, which is the reason
    this model lives here and not in accounts.

    The primary key is a uuid7 because a client id travels in URLs and payloads to
    a front end we do not control, and sequential ids would let anyone holding one
    both probe for neighbours and measure how fast the tenant acquires clients.

    Identity inside a tenant is the phone number. Not the name -- two people are
    called the same thing -- and not the email, because a family shares one and
    rejecting the second member would be a bug reported by an angry receptionist.
    """

    # One field, not first/last: name structure is not universal, and nothing in
    # the product needs the parts separately.
    name = models.CharField(max_length=120)
    # Stored in E.164 (+541112345678). The canonical format IS the feature: the
    # constraint below compares strings, so "11 1234-5678" and "+54 9 11 1234 5678"
    # would otherwise be two clients for the same person and the constraint would
    # pass without a word.
    phone = PhoneNumberField(blank=True)
    email = models.EmailField(blank=True)
    # Deliberately a plain field, not a Note model: phase 1 notes are trivial. A
    # model earns its place when someone needs to know who wrote what and when.
    notes = models.TextField(blank=True)
    # Answers to whatever this tenant decided to ask (see ClientField), keyed by
    # ClientField.key. Here and not in a value table because a client file is
    # always read whole and never searched across clients: one column, one read,
    # no fan-out. jsonb, so a GIN index is available the day filtering by an
    # answer is actually asked for.
    #
    # Nothing about the shape is enforced by the database. ClientSerializer
    # validates every key and value against this tenant's field definitions, and
    # is the only place that may write here.
    custom_data = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = 'tb_client'
        ordering = ('name',)
        constraints = [
            # Partial: a client without a phone is perfectly valid and there may be
            # many of them, so the empty string must not collide with itself.
            models.UniqueConstraint(
                fields=['tenant', 'phone'],
                condition=~models.Q(phone=''),
                name='unique_client_phone_per_tenant',
            ),
        ]

    def __str__(self):
        return self.name
