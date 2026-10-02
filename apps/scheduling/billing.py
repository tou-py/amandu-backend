"""
What a client owes and what a turn costs, decided in one place.

The operator never works out whether to charge: for every attendee of every
turn the API resolves exactly one billing state with one amount, and the front
end renders it without recomputing anything. Every rule that state depends on
lives in this module -- the anchored plan periods, which of them are paid, the
quota, the per-turn price -- so the turn dialog, the client file and the
receivables list cannot disagree about the same person.

Nothing here is stored. Coverage and debt are computed from Subscription rows
and the cash book every time they are asked, exactly as Client.plan_state used
to be: a stored "expired" flag needs a sweep to keep it true and is wrong
between ticks.

Periods. Period k of a subscription starts on its start date plus k months and
ends the day before period k+1 starts. Each start is counted from the ORIGINAL
start date through add_months, never from the previous period, so an anchor of
31 lands on the 28th/29th/30th of short months and goes back to the 31st after
them instead of drifting. A period is named after the month it starts in, and a
cash entry pays it by carrying its start date in `CashEntry.period`.

Phase 0 is the contract: the shapes below are final, and the parts marked
`# phase 1:` answer with something minimal but schema-valid until the rules
behind them land.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

from django.utils import timezone

from apps.commons.dates import add_months
from apps.tenancy.models import Tenant

# Spanish, unlike everything around it, for the same reason as the cash entry
# concepts: these are the words the operator reads ("Debe octubre"), not
# identifiers. Lower case, as Spanish writes month names mid-sentence.
MONTH_NAMES = (
    'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
    'septiembre', 'octubre', 'noviembre', 'diciembre',
)


class State:
    """The billing states, in evaluation order: the first that applies wins."""

    PAID = 'paid'
    PLAN_OWED = 'plan_owed'
    COVERED = 'covered'
    EXTRA = 'extra'
    CHARGE = 'charge'
    NO_PRICE = 'no_price'

    ALL = (PAID, PLAN_OWED, COVERED, EXTRA, CHARGE, NO_PRICE)


@dataclass(frozen=True)
class Period:
    """One period of one subscription, with what it costs and when it is due."""

    subscription: object
    start: date
    end: date
    amount: int
    due_date: date
    overdue: bool

    @property
    def name(self):
        return MONTH_NAMES[self.start.month - 1]


def local_today(tenant):
    """
    Today in the BUSINESS's calendar, not the server's. Every "has this period
    started", "is it overdue" is a question about a day the operator names, so
    it is asked against the date on their wall -- the same comparison the tenant
    sweep makes.
    """
    return timezone.localdate(timezone=ZoneInfo(tenant.timezone))


def default_start_date(tenant, today=None):
    """
    What a new subscription's start date is prefilled with: the next 1st under
    month_start (today itself when today is a 1st), today under join_day. A
    prefill and nothing more -- the subscription keeps whatever it was given.
    """
    today = today or local_today(tenant)
    if tenant.plan_period_start == Tenant.PlanPeriodStart.JOIN_DAY or today.day == 1:
        return today
    return add_months(today.replace(day=1), 1)


def period_start(subscription, k):
    return add_months(subscription.start_date, k)


def period_end(subscription, k):
    """The last day of period k: the day before period k+1 starts."""
    return period_start(subscription, k + 1) - timedelta(days=1)


def period_index(subscription, day):
    """Which period `day` falls in. -1 before the subscription starts."""
    start = subscription.start_date
    k = (day.year - start.year) * 12 + day.month - start.month
    return k - 1 if period_start(subscription, k) > day else k


def make_period(subscription, k, today):
    start = period_start(subscription, k)
    due = start + timedelta(days=subscription.tenant.plan_grace_days)
    return Period(
        subscription=subscription,
        start=start,
        end=period_end(subscription, k),
        amount=subscription.price,
        due_date=due,
        # Overdue AFTER the due date: the due date itself is still on time,
        # compared inclusive like every other day the operator names.
        overdue=today > due,
    )


def _paid_starts(subscription):
    return set(
        subscription.cash_entries.filter(voided_at__isnull=True, period__isnull=False)
        .values_list('period', flat=True)
    )


def unpaid_periods(client, today):
    """
    Every unpaid period of the client's subscriptions, oldest first: the owed
    ones, then -- for an open subscription -- the future ones, forever. A
    generator so a caller takes what it needs (`islice`) instead of this module
    guessing how far ahead anybody pays.

    A period belongs to a subscription only if it starts on or before its end
    date: ending a subscription is what stops it producing debt.
    """
    for subscription in client.subscriptions.select_related('plan', 'tenant').order_by('start_date'):
        paid = _paid_starts(subscription)
        k = 0
        while True:
            start = period_start(subscription, k)
            if subscription.end_date is not None and start > subscription.end_date:
                break
            if start not in paid:
                yield make_period(subscription, k, today)
            k += 1


def owed_periods(client, today):
    """Unpaid periods that have already started. Not all of them are overdue yet."""
    owed = []
    for period in unpaid_periods(client, today):
        if period.start > today:
            break
        owed.append(period)
    return owed


def periods_to_charge(client, count, today):
    """The next `count` unpaid periods, oldest owed first, into the future if needed."""
    return list(islice(unpaid_periods(client, today), count))


def last_paid_end(subscription):
    """The last day of the latest paid period, or None if nothing was ever paid."""
    paid = _paid_starts(subscription)
    if not paid:
        return None
    return period_end(subscription, period_index(subscription, max(paid)))


def current_subscription(client, today):
    """
    The subscription running today, else the next one about to start, else
    None. Ranges never overlap, so the earliest not-yet-finished one is it.
    """
    return (
        client.subscriptions.select_related('plan', 'tenant')
        .exclude(end_date__lt=today)
        .order_by('start_date')
        .first()
    )


def attendee_billing(link):
    """
    The billing state of one attendee of one turn, as the `billing` object on
    the appointment's roster (BillingSerializer).

    Reads `link.appointment.cash_entries.all()`, so a caller serialising many
    turns prefetches `cash_entries` and this costs no query per row.
    """
    answer = {
        'state': None, 'amount': None, 'owed_periods': [],
        'quota_used': None, 'quota_total': None,
        'payment_method': None, 'cash_entry': None,
    }
    payment = next(
        (
            entry for entry in link.appointment.cash_entries.all()
            if entry.client_id == link.client_id and entry.voided_at is None
        ),
        None,
    )
    if payment is not None:
        answer.update(
            state=State.PAID, amount=payment.amount,
            payment_method=payment.payment_method, cash_entry=payment.id,
        )
        return answer

    # phase 1: plan_owed (owed_periods against today), covered and extra (the
    # subscription covering the turn's period and category, and the quota
    # ordering that fills quota_used/quota_total). Until then a subscribed
    # client falls through to the per-turn price like everybody else.
    price = link.appointment.service.price
    if price is None:
        answer['state'] = State.NO_PRICE
    else:
        answer.update(state=State.CHARGE, amount=price)
    return answer


def billing_summary(client):
    """The money header of the client file (BillingSummarySerializer)."""
    today = local_today(client.tenant)
    subscription = current_subscription(client, today)
    current = None
    if subscription is not None:
        k = max(period_index(subscription, today), 0)
        current = {
            'id': subscription.id,
            'plan': subscription.plan_id,
            'plan_name': subscription.plan.name,
            'price': subscription.price,
            'price_override': subscription.price_override,
            'start_date': subscription.start_date,
            'end_date': subscription.end_date,
            'current_period': {
                'start': period_start(subscription, k),
                'end': period_end(subscription, k),
                'name': MONTH_NAMES[period_start(subscription, k).month - 1],
            },
            # phase 1: the quota -- turns in covered categories counted in this
            # period. Zero used and the plan's whole allowance left until then.
            'sessions_used': 0,
            'sessions_total': subscription.plan.sessions_per_period,
            'sessions_left': subscription.plan.sessions_per_period,
        }
    owed = owed_periods(client, today)
    # phase 1: unpaid turns since the go-live cutoff (the migration date), each
    # with the amount its billing state charges.
    unpaid_turns = []
    return {
        'subscription': current,
        'owed_periods': owed,
        'unpaid_turns': unpaid_turns,
        'total': sum(p.amount for p in owed) + sum(t['amount'] for t in unpaid_turns),
    }


def receivables(tenant):
    """
    Who owes, for the Por cobrar screen (ReceivablesSerializer): one row per
    client with overdue periods or unpaid turns, oldest debt first, then the
    clients whose current period falls due within the next three days.
    """
    # phase 1: the rows (overdue periods per client plus unpaid turns since the
    # go-live cutoff, ordered by oldest debt), the due-soon section (current
    # period unpaid and due within DUE_SOON_DAYS) and their total.
    return {'total': 0, 'rows': [], 'due_soon': []}


# Fixed, not configurable (see the spec's Out of Scope).
DUE_SOON_DAYS = 3


def concept_for_period(client, period):
    # Spanish: the line the shop reads in its cash book, next to the ones the
    # front end writes. Same separator as those.
    return f'Mensualidad · {client.name} · {period.name} {period.start.year}'


def concept_for_turn(client, appointment):
    return f'Turno · {appointment.service.name} · {client.name}'

