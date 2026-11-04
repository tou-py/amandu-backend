"""The rules of where money is. The saldo lives here and nowhere else."""
from django.db.models import BigIntegerField, Case, F, Q, Sum, Value, When

from apps.accounting.models import Caja, CashEntry, MethodCaja
from apps.commons.errors import Refused


def saldo(caja, day):
    """
    What a caja should hold at the end of `day`: the amount counted when it opened,
    plus each movement on its own day, minus each undone one on the day it was undone.

    Movements dated before the opening stay outside on both counts. They were never
    added, so undoing one must not subtract: what was counted is the base.
    """
    signed = Case(
        When(kind=CashEntry.Kind.INCOME, then=F('amount')),
        default=F('amount') * Value(-1),
        output_field=BigIntegerField(),
    )
    totals = CashEntry.objects.filter(caja=caja, occurred_on__gte=caja.opened_on).aggregate(
        moved=Sum(signed, filter=Q(occurred_on__lte=day)),
        undone=Sum(signed, filter=Q(voided_on__lte=day)),
    )
    return caja.opening_balance + (totals['moved'] or 0) - (totals['undone'] or 0)


def refuse_before_opening(caja, day):
    if day < caja.opened_on:
        raise Refused('That day is before this caja opened.', 'before_caja_opened')


def caja_for_income(tenant, payment_method, day, asked=None):
    """
    Where a charge lands: the caja the counter chose, or the one its medio de pago
    maps to, which is how a PWA that knows nothing of cajas keeps working. None
    while Caja is off: nothing can say where the money ended up.
    """
    if not tenant.addon_caja:
        return None
    if asked is None:
        caja = MethodCaja.objects.select_related('caja').get(tenant=tenant, payment_method=payment_method).caja
    else:
        caja = Caja.objects.for_tenant(tenant).filter(pk=asked, archived=False).first()
        if caja is None:
            raise Refused('That caja does not exist.', 'unknown_caja')
    refuse_before_opening(caja, day)
    return caja
