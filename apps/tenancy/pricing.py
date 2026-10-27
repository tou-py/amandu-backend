"""
What a tenant pays Kyo, and where it stands on paying it.

Prices are constants because they are the same for every tenant; a table earns
its place the day one tenant pays something different. The plan is not stored
either: Solo and Equipo are names for "one professional" and "more than one",
so the plan follows the team instead of being a second fact that can disagree
with it.

Amounts are guaraníes, whole numbers, like every price in the system.
"""
from datetime import date, timedelta

from django.conf import settings

BASE = 100_000  # Solo: the agenda with one professional attending.
PER_EXTRA_PROFESSIONAL = 50_000  # Each professional after the first.
COBROS = 80_000  # Monthly plans, charges and Por cobrar.
FACTURACION = 125_000  # Electronic invoicing, documents included below.
FACTURACION_INCLUDED = 1_000  # Documents a month inside FACTURACION.
FACTURACION_EXTRA = 7_000  # Each document past the included ones.

# Days before paid_until the owner starts being told, and days after it the
# tenant keeps reading but can no longer write. The sweep suspends after that.
NOTICE_DAYS = 3
READ_ONLY_DAYS = 3

OK = 'ok'
DUE = 'due'
READ_ONLY = 'read_only'
SUSPENDED = 'suspended'
CLOSED = 'closed'


def billing_state(tenant, today: date) -> str:
    """
    One word for where the tenant stands, read by the permission, /me and the
    notice. The status wins over the dates: a tenant suspended by hand for a
    reason that is not money is suspended whatever paid_until says, and NULL
    paid_until (pilots, courtesy accounts) is never due.
    """
    if tenant.status == tenant.Status.CLOSED:
        return CLOSED
    if tenant.status == tenant.Status.SUSPENDED:
        return SUSPENDED
    if tenant.paid_until is None:
        return OK
    if today > tenant.paid_until:
        # Past the window the sweep has not run yet (it runs every five
        # minutes); still read-only rather than pretending to be suspended.
        return READ_ONLY
    if today >= tenant.paid_until - timedelta(days=NOTICE_DAYS):
        return DUE
    return OK


def suspends_after(tenant) -> date | None:
    """The last day of the read-only window; the sweep suspends the day after."""
    if tenant.paid_until is None:
        return None
    return tenant.paid_until + timedelta(days=READ_ONLY_DAYS)


def addons(tenant) -> list[str]:
    return [
        name
        for name, on in (('cobros', tenant.addon_cobros), ('facturacion', tenant.addon_facturacion))
        if on
    ]


def plan_name(professionals: int) -> str:
    return 'equipo' if professionals > 1 else 'solo'


def monthly_lines(tenant, professionals: int) -> list[dict]:
    """
    What the month costs, line by line, as the subscription panel shows it.

    A tenant whose only member does not attend appointments still pays the
    base: the base is the agenda, and the first professional comes inside it.
    """
    extra = max(professionals - 1, 0)
    lines = [{'concept': 'base', 'quantity': 1, 'amount': BASE}]
    if extra:
        lines.append({
            'concept': 'extra_professionals',
            'quantity': extra,
            'amount': extra * PER_EXTRA_PROFESSIONAL,
        })
    if tenant.addon_cobros:
        lines.append({'concept': 'cobros', 'quantity': 1, 'amount': COBROS})
    if tenant.addon_facturacion:
        # ponytail: the base only. Documents past FACTURACION_INCLUDED are
        # billed once invoicing counts approved documents per month.
        lines.append({'concept': 'facturacion', 'quantity': 1, 'amount': FACTURACION})
    return lines


def payment_instructions() -> str:
    return getattr(settings, 'KYO_PAYMENT_INSTRUCTIONS', '')


def contact_whatsapp() -> str:
    return getattr(settings, 'KYO_CONTACT_WHATSAPP', '')
