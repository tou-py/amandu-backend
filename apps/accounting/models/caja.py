from django.db import models

from apps.commons.mixins import TimestampMixin
from apps.tenancy.mixins import TenantOwnedMixin

from .cash_entry import CashEntry


class Caja(TenantOwnedMixin, TimestampMixin):
    """
    A place where the business keeps money and can count it. Its saldo is always
    worked out from these two facts and its movements (apps/accounting/cajas.py),
    never stored: a stored figure drifts, and nothing would say which to believe.
    """

    name = models.CharField(max_length=60)
    # The day somebody counted `opening_balance`. Set by the server to that day,
    # never typed: back-dating it would pull money nobody counted into the saldo.
    opened_on = models.DateField(editable=False)
    opening_balance = models.PositiveBigIntegerField()
    archived = models.BooleanField(default=False, editable=False)

    class Meta:
        db_table = 'tb_caja'
        ordering = ('created_at', 'id')

    def __str__(self):
        return self.name


class MethodCaja(TenantOwnedMixin, TimestampMixin):
    """Where money taken by each medio de pago lands, so charging stays one tap short."""

    payment_method = models.CharField(max_length=20, choices=CashEntry.PaymentMethod.choices)  # type: ignore
    # PROTECT: a charge must always find a caja, so one in use cannot go away.
    caja = models.ForeignKey(Caja, on_delete=models.PROTECT, related_name='methods')

    class Meta:
        db_table = 'tb_method_caja'
        constraints = [
            models.UniqueConstraint(fields=['tenant', 'payment_method'], name='one_caja_per_method'),
        ]

    def __str__(self):
        return f'{self.payment_method} -> {self.caja_id}'
