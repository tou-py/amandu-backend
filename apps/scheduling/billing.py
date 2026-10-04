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

Quota. Within one period of a subscription, the client's turns in the plan's
categories (not cancelled, not a pending request) are ordered by start time;
the first N are covered and the rest are extra. No-shows count, because the
booking held the slot. The
order is total (start, then id), so the same turn always gets the same answer --
and booking a turn earlier in the period can push a later one into extra, which
is the honest reading of "8 per month".

Reading a whole roster costs a fixed number of queries, never one per
attendee: everything below reads a client through `client_prefetches()`, which
a list view prefetches once per page and a single call loads on demand.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from itertools import islice
from operator import attrgetter
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import (
    DateTimeField, ExpressionWrapper, OuterRef, Prefetch, Subquery, Value, prefetch_related_objects,
)
from django.db.models.functions import Least
from django.utils import timezone

from apps.commons.dates import add_months
from apps.scheduling.models import Appointment, AppointmentClient, Client, Subscription
from apps.tenancy.models import Tenant

# Spanish, unlike everything around it, for the same reason as the cash entry
# concepts: these are the words the operator reads ("Debe octubre"), not
# identifiers. Lower case, as Spanish writes month names mid-sentence.
MONTH_NAMES = (
    'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto',
    'septiembre', 'octubre', 'noviembre', 'diciembre',
)

# Fixed, not configurable (see the spec's Out of Scope).
DUE_SOON_DAYS = 3


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


def _day(moment, tenant):
    """The business's calendar day a turn happens on."""
    return moment.astimezone(ZoneInfo(tenant.timezone)).date()


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


def client_prefetches(path=''):
    """
    Everything billing reads off a client, as prefetch lookups rooted at `path`
    (e.g. 'client_links__client__' from an appointment). One list, used by the
    agenda, the client file and Por cobrar alike, so no reader can forget a
    relation and quietly fall back to a query per attendee.
    """
    first_start = (
        Subscription.objects.filter(client=OuterRef('client'))
        .order_by('start_date').values('start_date')[:1]
    )
    return [
        Prefetch(
            f'{path}subscriptions',
            queryset=Subscription.objects.select_related('plan', 'tenant')
            .prefetch_related('plan__categories', 'cash_entries'),
        ),
        Prefetch(
            f'{path}appointment_links',
            queryset=AppointmentClient.objects
            # A pending public request is not a turn until the shop accepts it.
            .exclude(appointment__status__in=(Appointment.Status.CANCELLED, Appointment.Status.PENDING))
            # Only the turns a rule can still ask about: from the first
            # subscription (the quota) or the go-live cutoff (unpaid turns),
            # whichever is earlier, less a day so a business east of UTC does not
            # lose its first morning to the date-to-timestamp cast. LEAST skips
            # the NULL of a client who never subscribed.
            #
            # ponytail: a subscribed client's whole history since joining is
            # read to rank one turn. Fine at a few hundred turns per client; past
            # that, bound it by the earliest period on screen.
            .filter(appointment__start__gte=ExpressionWrapper(
                Least(Subquery(first_start), Value(settings.BILLING_GO_LIVE))
                - Value(timedelta(days=1)),
                output_field=DateTimeField(),
            ))
            .select_related('appointment__service')
            .prefetch_related('appointment__cash_entries'),
        ),
    ]


def _load(client):
    # A no-op on a client the caller already prefetched; one query per relation
    # on a bare one (a single charge, a single client file).
    prefetch_related_objects([client], *client_prefetches())


def _subscriptions(client):
    return sorted(client.subscriptions.all(), key=attrgetter('start_date'))


def _paid_starts(subscription):
    # Filtered here rather than in the query, so a prefetched `cash_entries`
    # answers it without another round trip.
    return {
        entry.period for entry in subscription.cash_entries.all()
        if entry.voided_at is None and entry.period is not None
    }


def unpaid_periods(client, today):
    """
    Every unpaid period of the client's subscriptions, oldest first: the owed
    ones, then -- for an open subscription -- the future ones, forever. A
    generator so a caller takes what it needs (`islice`) instead of this module
    guessing how far ahead anybody pays.

    A period belongs to a subscription only if it starts on or before its end
    date: ending a subscription is what stops it producing debt.
    """
    _load(client)
    for subscription in _subscriptions(client):
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
    return next(
        (s for s in _subscriptions(client) if s.end_date is None or s.end_date >= today), None
    )


def _covered_categories(subscription):
    # Empty means ALL (Plan.categories): an empty set here is the widest plan.
    return {category.id for category in subscription.plan.categories.all()}


def _counted(client, subscription, k):
    """
    The client's turns that count against period k of `subscription`.
    Cancelled ones and pending requests are already out (client_prefetches);
    no-shows stay in.
    """
    start, end = period_start(subscription, k), period_end(subscription, k)
    categories = _covered_categories(subscription)
    return [
        link for link in client.appointment_links.all()
        if (not categories or link.appointment.service.category_id in categories)
        and start <= _day(link.appointment.start, subscription.tenant) <= end
    ]


def _payment(link):
    return next(
        (
            entry for entry in link.appointment.cash_entries.all()
            if entry.client_id == link.client_id and entry.voided_at is None
        ),
        None,
    )


def _turn_state(link):
    """
    covered, extra, charge or no_price: what this turn itself costs, before a
    payment or a plan debt is looked at. Kept apart from attendee_billing so an
    unpaid turn is judged on the turn -- a client who owes September still owes
    Tuesday's extra, and the plan debt must not hide it.
    """
    appointment = link.appointment
    price = appointment.service.price
    subscription = next(
        (
            s for s in _subscriptions(link.client)
            if s.start_date <= _day(appointment.start, s.tenant)
            and (s.end_date is None or _day(appointment.start, s.tenant) <= s.end_date)
        ),
        None,
    )
    if subscription is not None:
        categories = _covered_categories(subscription)
        if not categories or appointment.service.category_id in categories:
            k = period_index(subscription, _day(appointment.start, subscription.tenant))
            # Its place in quota order -- start time, then id, so two turns at
            # the same minute still get one answer each -- counted rather than
            # looked up: one past every turn ahead of it, which also answers for
            # a turn not in the list itself (a cancelled one).
            here = (appointment.start, appointment.pk)
            used = 1 + sum(
                1 for other in _counted(link.client, subscription, k)
                if (other.appointment.start, other.appointment_id) < here
            )
            total = subscription.plan.sessions_per_period
            quota = {'quota_used': used, 'quota_total': total}
            if total is None or used <= total:
                return {'state': State.COVERED, **quota}
            if price is not None:
                return {'state': State.EXTRA, 'amount': price, **quota}
        elif price is not None:
            return {'state': State.EXTRA, 'amount': price}
    if price is None:
        return {'state': State.NO_PRICE}
    return {'state': State.CHARGE, 'amount': price}


def attendee_billing(link):
    """
    The billing state of one attendee of one turn, as the `billing` object on
    the appointment's roster (BillingSerializer).

    Reads `link.appointment.cash_entries.all()` and the client through
    client_prefetches(), so a caller serialising many turns prefetches both and
    this costs no query per row.
    """
    answer = {
        'state': None, 'amount': None, 'owed_periods': [],
        'quota_used': None, 'quota_total': None,
        'payment_method': None, 'cash_entry': None,
    }
    payment = _payment(link)
    if payment is not None:
        answer.update(
            state=State.PAID, amount=payment.amount,
            payment_method=payment.payment_method, cash_entry=payment.id,
        )
        return answer

    client = link.client
    _load(client)
    subscriptions = _subscriptions(client)
    if subscriptions:
        # Against TODAY, not the turn's period: whoever owes September is told
        # so on an October turn, because that is when they are at the counter.
        # Only the overdue periods: one still inside its grace days is not yet
        # a debt to chase at the door.
        today = local_today(subscriptions[0].tenant)
        overdue = [p for p in owed_periods(client, today) if p.overdue]
        if overdue:
            answer.update(
                state=State.PLAN_OWED, amount=sum(p.amount for p in overdue), owed_periods=overdue,
            )
            return answer

    answer.update(_turn_state(link))
    return answer


def _unpaid_turns(client, tenant):
    """
    The client's past turns nobody paid for that the turn itself charges for
    (charge or extra), oldest first. Only from the go-live cutoff: history from
    before per-turn charging existed was never meant to be collected, and would
    flood Por cobrar with debts nobody can reconstruct. A no-show spent its
    quota but is not chased as a per-turn charge.
    """
    now = timezone.now()
    turns = []
    for link in sorted(client.appointment_links.all(), key=lambda one: one.appointment.start):
        appointment = link.appointment
        if appointment.start >= now or _day(appointment.start, tenant) < settings.BILLING_GO_LIVE:
            continue
        if _payment(link) is not None or link.attendance == AppointmentClient.Attendance.NO_SHOW:
            continue
        state = _turn_state(link)
        if state['state'] in (State.CHARGE, State.EXTRA):
            turns.append({
                'appointment': appointment.pk,
                'start': appointment.start,
                'service_name': appointment.service.name,
                'amount': state['amount'],
            })
    return turns


def billing_summary(client):
    """The money header of the client file (BillingSummarySerializer)."""
    today = local_today(client.tenant)
    _load(client)
    subscription = current_subscription(client, today)
    current = None
    if subscription is not None:
        k = max(period_index(subscription, today), 0)
        # Every booked turn in the period, past and future: the booking holds
        # the quota, the same count that numbers each turn "5 de 8". So what is
        # left is what can still be BOOKED ("quedan 3"), and `booked_ahead` is
        # how much of `used` has not happened yet ("2 reservadas").
        counted = _counted(client, subscription, k)
        used = len(counted)
        now = timezone.now()
        total = subscription.plan.sessions_per_period
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
            'sessions_used': used,
            'booked_ahead': sum(1 for link in counted if link.appointment.start > now),
            'sessions_total': total,
            'sessions_left': None if total is None else max(total - used, 0),
        }
    owed = owed_periods(client, today)
    unpaid_turns = _unpaid_turns(client, client.tenant)
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
    today = local_today(tenant)
    rows, due_soon = [], []
    # ponytail: every client of the tenant, judged in Python. A fixed number of
    # queries, but linear in clients; filter to those with a subscription or a
    # turn since the cutoff when a tenant grows into the thousands.
    for client in Client.objects.for_tenant(tenant).prefetch_related(*client_prefetches()):
        owed = owed_periods(client, today)
        overdue = [p for p in owed if p.overdue]
        turns = _unpaid_turns(client, tenant)
        if overdue or turns:
            rows.append({
                'client': client.pk,
                'client_name': client.name,
                'client_phone': str(client.phone),
                # The subscription still producing the debt, for "End plan".
                'subscription': overdue[-1].subscription.id if overdue else None,
                'overdue_periods': overdue,
                'unpaid_turns': turns,
                'amount': sum(p.amount for p in overdue) + sum(t['amount'] for t in turns),
                'oldest_debt': min(
                    [p.start for p in overdue] + [_day(t['start'], tenant) for t in turns]
                ),
            })
        # Only the latest owed period can be due soon: any older unpaid one is
        # already past its due date, since grace stops short of the next period.
        if owed and not owed[-1].overdue and owed[-1].due_date <= today + timedelta(days=DUE_SOON_DAYS):
            due_soon.append({
                'client': client.pk,
                'client_name': client.name,
                'client_phone': str(client.phone),
                'subscription': owed[-1].subscription.id,
                'period': owed[-1],
            })
    rows.sort(key=lambda row: (row['oldest_debt'], row['client_name']))
    due_soon.sort(key=lambda row: (row['period'].due_date, row['client_name']))
    return {'total': sum(row['amount'] for row in rows), 'rows': rows, 'due_soon': due_soon}


def concept_for_period(client, period):
    # Spanish: the line the shop reads in its cash book, next to the ones the
    # front end writes. Same separator as those.
    return f'Mensualidad · {client.name} · {period.name} {period.start.year}'


def concept_for_turn(client, appointment):
    return f'Turno · {appointment.service.name} · {client.name}'
