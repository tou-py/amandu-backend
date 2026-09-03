from django.db import models
from django.utils import timezone
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
    # What a month of this client's plan costs, in whole guaraníes. PYG has no
    # fractional unit, so the integer IS the exact amount -- same reasoning as
    # CashEntry.amount, and do NOT "fix" it into a DecimalField.
    monthly_fee = models.PositiveBigIntegerField(null=True, blank=True)
    # The last day this client's plan covers.
    #
    # READ THIS BEFORE REASONING FROM Tenant.paid_until: the field has the same
    # name there and the OPPOSITE null semantics. On a tenant, NULL means "never
    # expires" -- a courtesy account that is never swept. On a client, NULL means
    # "has no plan at all": this person pays per session, which is every client
    # of a salon. Reading a client NULL as "never expires" would hand a free
    # standing subscription to everybody who never bought one, and would do it
    # silently, so plan_state() below is the only thing that should ever read
    # this column.
    #
    # There is no "expired" status anywhere and there must not be one. Whether a
    # plan still covers today is a comparison against the calendar, right the
    # moment it is asked; a stored flag would need a sweep to keep it true and
    # would be wrong between ticks. This is NOT the tenant case, where the sweep
    # exists because expiry has to MUTATE something -- it suspends. Nothing is
    # suspended here.
    paid_until = models.DateField(null=True, blank=True)

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

    # Named here because plan_state() is what produces them and two serializers
    # spend them; a literal in each is a fourth state away from disagreeing.
    PLAN_STATES = (('none', 'None'), ('active', 'Active'), ('expired', 'Expired'))

    def plan_state(self):
        """
        Whether a monthly plan covers this client: 'none' when there is no plan
        and they pay per session, 'active' while it runs, 'expired' once it has
        run out.

        One definition, in one place, on purpose. The API answers with it and the
        front end decides from it whether the charge step appears at all, and two
        copies of the same rule drift apart -- Membership.can_schedule_for_others
        exists for the same reason.

        Compared against today and not against now, exactly as the tenant sweep
        does it: `paid_until` is a day the operator names, not an instant, so a
        client paid THROUGH today is still covered for all of today.
        """
        if self.paid_until is None:
            return 'none'
        return 'active' if self.paid_until >= timezone.localdate() else 'expired'

    def __str__(self):
        return self.name
