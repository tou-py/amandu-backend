from datetime import date

import pytest
from django.utils import timezone

from apps.accounting.models import Caja, CashEntry
from apps.accounting.cajas import saldo
from apps.tenancy.models import Tenant

OPENED = date(2026, 11, 2)


@pytest.fixture
def drawer(db):
    tenant = Tenant.objects.create(
        name='Salon', slug='salon', country='PY', addon_cobros=True, addon_caja=True
    )
    return Caja.objects.create(tenant=tenant, name='Principal', opened_on=OPENED, opening_balance=1000)


def move(caja, kind, amount, day=OPENED, voided_on=None):
    return CashEntry.objects.create(
        tenant=caja.tenant, caja=caja, kind=kind, amount=amount, occurred_on=day,
        concept='x', category='rent' if kind == 'expense' else '',
        voided_on=voided_on, voided_at=timezone.now() if voided_on else None,
    )


def test_starts_from_what_was_counted(drawer):
    assert saldo(drawer, OPENED) == 1000


def test_entradas_add_and_salidas_subtract_from_the_opening_day_on(drawer):
    move(drawer, 'income', 300)
    move(drawer, 'expense', 50, day=date(2026, 11, 3))
    assert saldo(drawer, OPENED) == 1300
    assert saldo(drawer, date(2026, 11, 3)) == 1250


def test_money_before_the_opening_stays_outside(drawer):
    move(drawer, 'income', 300, day=date(2026, 11, 1))
    assert saldo(drawer, OPENED) == 1000


def test_a_void_of_money_before_the_opening_reverses_nothing(drawer):
    # It was never added, and what was counted on the opening day is the truth.
    move(drawer, 'income', 300, day=date(2026, 11, 1), voided_on=date(2026, 11, 5))
    assert saldo(drawer, date(2026, 11, 5)) == 1000


def test_a_void_in_the_same_period_nets_out(drawer):
    move(drawer, 'income', 300, voided_on=OPENED)
    move(drawer, 'expense', 40, voided_on=OPENED)
    assert saldo(drawer, OPENED) == 1000


def test_a_later_void_leaves_the_earlier_day_as_it_was(drawer):
    move(drawer, 'income', 300, voided_on=date(2026, 11, 9))
    move(drawer, 'expense', 40, voided_on=date(2026, 11, 9))
    assert saldo(drawer, date(2026, 11, 8)) == 1260
    assert saldo(drawer, date(2026, 11, 9)) == 1000


def test_another_cajas_money_and_money_with_no_caja_do_not_count(drawer):
    other = Caja.objects.create(tenant=drawer.tenant, name='Banco', opened_on=OPENED, opening_balance=0)
    move(other, 'income', 300)
    CashEntry.objects.create(
        tenant=drawer.tenant, kind='income', amount=70, occurred_on=OPENED, concept='old till'
    )
    assert saldo(drawer, OPENED) == 1000
    assert saldo(other, OPENED) == 300


def test_equals_the_opening_plus_the_live_rows_of_the_list(drawer):
    move(drawer, 'income', 300)
    move(drawer, 'income', 20, voided_on=date(2026, 11, 3))
    move(drawer, 'expense', 50, day=date(2026, 11, 3))
    rows = CashEntry.objects.filter(caja=drawer, voided_at__isnull=True)
    listed = sum(row.amount if row.kind == 'income' else -row.amount for row in rows)
    assert saldo(drawer, date(2026, 11, 3)) == drawer.opening_balance + listed
