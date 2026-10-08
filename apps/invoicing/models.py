import json

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin


def _fernet():
    if not settings.INVOICING_ENCRYPTION_KEY:
        raise ImproperlyConfigured('INVOICING_ENCRYPTION_KEY is unset; invoicing credentials cannot be stored.')
    return Fernet(settings.INVOICING_ENCRYPTION_KEY)


class InvoicingAccount(TimestampMixin):
    """
    A business's account at its invoicing provider, and the numbering it issues
    under. One per tenant.

    The numbering lives here and not at the provider because SIFEN leaves it to
    the issuer: the timbrado authorises a range per establishment and point of
    issue, and gaps have to be formally voided. `next_number` is only ever
    advanced under a row lock, by the code that also creates the Invoice.
    """

    tenant = models.OneToOneField(
        'tenancy.Tenant', on_delete=models.CASCADE, related_name='invoicing_account', editable=False,
    )
    # A key of apps.invoicing.providers.PROVIDERS. Not `choices=`: a new
    # provider must not need a migration, and the serializer validates it.
    provider = models.CharField(max_length=40)
    # Whatever the provider needs to act as this business (an API key, for
    # SifenSIS), as encrypted JSON. A key that issues fiscal documents in the
    # business's name is not something a database dump should hand over.
    credentials_encrypted = models.TextField(blank=True, editable=False)
    # Codes as SIFEN prints them on the KuDE: three digits, zero padded.
    establishment = models.CharField(max_length=3, default='001')
    point = models.CharField(max_length=3, default='001')
    next_number = models.PositiveIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        db_table = 'tb_invoicing_account'

    def __str__(self):
        return f'{self.tenant} · {self.provider}'

    @property
    def credentials(self):
        if not self.credentials_encrypted:
            return {}
        return json.loads(_fernet().decrypt(self.credentials_encrypted.encode()))

    @credentials.setter
    def credentials(self, value):
        self.credentials_encrypted = _fernet().encrypt(json.dumps(value).encode()).decode()


class Invoice(TenantOwnedMixin, TimestampMixin):
    """
    One electronic invoice for one income in the cash book.

    The row exists as soon as a number is spent on it. That is why SENDING
    exists: when the provider did not answer, the document may or may not have
    been issued, and the number has to stay taken until a retry finds out.
    """

    class Status(models.TextChoices):
        # Sent, no answer yet. The sweep re-sends the same draft (the provider
        # recognises a duplicate and returns the original).
        SENDING = 'sending', 'Sending'
        # Accepted by the provider, waiting on the SET.
        PENDING = 'pending', 'Pending'
        APPROVED = 'approved', 'Approved'
        REJECTED = 'rejected', 'Rejected'
        CANCELLED = 'cancelled', 'Cancelled'

    # PROTECT: the cash book is never deleted from, and a fiscal document must
    # not lose the money it documents.
    cash_entry = models.OneToOneField(
        'accounting.CashEntry', on_delete=models.PROTECT, related_name='invoice',
    )
    # Who issued it, at the time. An account can move to another provider; its
    # past invoices still live at the old one.
    provider = models.CharField(max_length=40)
    establishment = models.CharField(max_length=3)
    point = models.CharField(max_length=3)
    number = models.PositiveIntegerField()
    # The providers.Draft as sent, so a retry is byte-for-byte the same document.
    draft = models.JSONField()
    total = models.PositiveBigIntegerField()
    customer_tax_id = models.CharField(max_length=20, blank=True)
    customer_name = models.CharField(max_length=120)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENDING)  # type: ignore
    # The provider's handle: the 44-digit CDC for SIFEN.
    reference = models.CharField(max_length=64, blank=True)
    # The provider's or the SET's last word, shown on a rejection.
    message = models.TextField(blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    class Meta:
        db_table = 'tb_invoice'
        ordering = ('-created_at',)
        constraints = [
            models.UniqueConstraint(
                fields=['tenant', 'establishment', 'point', 'number'],
                name='one_invoice_per_number',
            ),
        ]

    def __str__(self):
        return f'{self.establishment}-{self.point}-{self.number:07d}'
