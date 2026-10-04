"""
Charging and voiding, and the billing shapes the front end renders: the
`billing` object on every attendee, the client file's `billing_summary` and
the receivables list. Driven through the HTTP API only.

Days are relative to the real today wherever the rule is about "now" (owed,
overdue, due soon), and absolute and in the past wherever it is about the
calendar (an anchor of 31 across February), so the suite holds on any day.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.commons.dates import add_months
from apps.scheduling.billing import local_today
from apps.scheduling.models import Appointment, AppointmentClient, Category, Client, Plan, Service, Subscription
from apps.tenancy.models import Tenant

RECEIVABLES = reverse('scheduling:receivables')
SUBSCRIPTIONS = reverse('scheduling:subscription-list')
YESTERDAY = timezone.now().replace(microsecond=0) - timedelta(days=1)


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


def client_url(client, name=None):
    if name is None:
        return reverse('scheduling:client-detail', args=[client.pk])
    return reverse(f'scheduling:client-{name}', args=[client.pk])


def charge_url(appointment):
    return reverse('scheduling:appointment-charge', args=[appointment.pk])


def void_url(entry):
    return reverse('accounting:cashentry-void', args=[entry.pk])


@pytest.fixture
def studio(db):
    return Tenant.objects.create(name='Studio', slug='studio', country='PY')


@pytest.fixture
def gym(db):
    return Tenant.objects.create(name='Gym', slug='gym', country='PY')


def member(django_user_model, tenant, role, email, attends=False):
    user = django_user_model.objects.create_user(email=email, password='pw')
    membership = Membership.objects.create(
        user=user, tenant=tenant, role=role, attends_appointments=attends
    )
    return user, membership


@pytest.fixture
def owner(django_user_model, studio):
    return member(django_user_model, studio, Membership.Role.OWNER, 'o@example.com')[0]


@pytest.fixture
def staff(django_user_model, studio):
    """A stylist who does not own the turn being charged."""
    return member(django_user_model, studio, Membership.Role.STAFF, 's@example.com')[0]


@pytest.fixture
def teacher(django_user_model, studio):
    return member(
        django_user_model, studio, Membership.Role.STAFF, 't@example.com', attends=True
    )[1]


@pytest.fixture
def ada(studio):
    return Client.objects.create(tenant=studio, name='Ada', phone='+595981123456')


@pytest.fixture
def bob(studio):
    return Client.objects.create(tenant=studio, name='Bob')


def turn(tenant, professional, clients, price=80000, start=YESTERDAY, category=None):
    service = Service.objects.create(
        tenant=tenant, name=f'Masaje {start.isoformat()}', price=price,
        duration=timedelta(minutes=60), category=category,
    )
    appointment = Appointment.objects.create(
        tenant=tenant, professional=professional, service=service,
        start=start, end=start + service.duration,
    )
    appointment.clients.add(*clients)
    return appointment


def billing_of(http, appointment):
    res = http.get(reverse('scheduling:appointment-detail', args=[appointment.pk]))
    return {a['name']: a['billing'] for a in res.data['attendees']}


# -- The per-attendee billing object -------------------------------------------


def test_an_attendee_of_a_priced_turn_is_to_be_charged(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])

    assert billing_of(api(owner, studio), appointment)['Ada'] == {
        'state': 'charge', 'amount': 80000, 'owed_periods': [],
        'quota_used': None, 'quota_total': None,
        'payment_method': None, 'cash_entry': None,
    }


def test_an_attendee_of_an_unpriced_turn_has_no_price(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada], price=None)

    assert billing_of(api(owner, studio), appointment)['Ada']['state'] == 'no_price'
    assert billing_of(api(owner, studio), appointment)['Ada']['amount'] is None


# -- Charging a turn -----------------------------------------------------------


def test_charging_a_turn_files_a_payment_and_shows_the_attendee_paid(
    staff, studio, teacher, ada, bob
):
    """Any member charges, even for a colleague's turn; the others in the
    group class are untouched."""
    appointment = turn(studio, teacher, [ada, bob])
    http = api(staff, studio)

    res = http.post(charge_url(appointment), {'client': str(ada.pk), 'payment_method': 'transfer'}, format='json')

    assert res.status_code == 201
    assert res.data['amount'] == 80000
    assert res.data['client'] == ada.pk
    assert res.data['appointment'] == appointment.pk
    assert res.data['occurred_on'] == local_today(studio).isoformat()
    billing = billing_of(http, appointment)
    assert billing['Ada']['state'] == 'paid'
    assert billing['Ada']['payment_method'] == 'transfer'
    assert billing['Ada']['cash_entry'] == res.data['id']
    assert billing['Bob']['state'] == 'charge'


def test_the_counter_can_edit_the_amount_and_the_day(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])

    res = api(owner, studio).post(
        charge_url(appointment),
        {'client': str(ada.pk), 'amount': 70000, 'occurred_on': '2026-09-30'},
        format='json',
    )

    assert res.data['amount'] == 70000
    assert res.data['occurred_on'] == '2026-09-30'
    assert res.data['payment_method'] == 'cash'


def test_staff_charge_the_price_but_do_not_edit_it(staff, studio, teacher, ada, bob):
    appointment = turn(studio, teacher, [ada, bob])
    unpriced = turn(studio, teacher, [ada], price=None, start=YESTERDAY - timedelta(hours=2))
    http = api(staff, studio)

    edited = http.post(charge_url(appointment), {'client': str(ada.pk), 'amount': 70000}, format='json')
    # The amount the form prefilled, sent back unchanged, is not an edit.
    same = http.post(charge_url(appointment), {'client': str(bob.pk), 'amount': 80000}, format='json')
    # A turn with no price has nothing to edit: whoever charges says how much.
    typed = http.post(charge_url(unpriced), {'client': str(ada.pk), 'amount': 50000}, format='json')

    assert (edited.status_code, edited.data['code']) == (403, 'amount_edit_forbidden')
    assert same.status_code == 201
    assert typed.status_code == 201


def test_an_unpriced_turn_needs_an_amount(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada], price=None)
    http = api(owner, studio)

    res = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')
    assert (res.status_code, res.data['code']) == (400, 'amount_required')
    assert http.post(
        charge_url(appointment), {'client': str(ada.pk), 'amount': 50000}, format='json'
    ).status_code == 201


def test_a_paid_attendee_cannot_be_charged_twice(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])
    http = api(owner, studio)
    http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    res = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    assert (res.status_code, res.data['code']) == (409, 'already_paid')
    assert CashEntry.objects.count() == 1


def test_only_someone_on_the_roster_is_charged(owner, studio, teacher, ada, bob):
    appointment = turn(studio, teacher, [ada])

    res = api(owner, studio).post(charge_url(appointment), {'client': str(bob.pk)}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'not_an_attendee')


@pytest.mark.parametrize('status', ['cancelled', 'pending'])
def test_a_cancelled_or_pending_turn_cannot_be_charged(owner, studio, teacher, ada, status):
    """Nothing happened (or nobody accepted it yet), so there is nothing to pay for."""
    appointment = turn(studio, teacher, [ada])
    Appointment.objects.filter(pk=appointment.pk).update(status=status)

    res = api(owner, studio).post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    assert (res.status_code, res.data['code']) == (409, 'turn_not_chargeable')
    assert not CashEntry.objects.exists()


def test_another_tenants_turn_cannot_be_charged(django_user_model, studio, gym, teacher, ada):
    appointment = turn(studio, teacher, [ada])
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    res = api(outsider, gym).post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    assert res.status_code == 404
    assert not CashEntry.objects.exists()


# -- Voiding -------------------------------------------------------------------


def test_an_owner_voids_a_payment_and_the_attendee_owes_again(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])
    http = api(owner, studio)
    paid = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json').data

    res = http.post(void_url(CashEntry.objects.get(pk=paid['id'])), {'reason': 'Cobro duplicado'}, format='json')

    assert res.status_code == 200
    assert res.data['voided_at'] is not None
    assert res.data['void_reason'] == 'Cobro duplicado'
    assert billing_of(http, appointment)['Ada']['state'] == 'charge'
    # The void is the undo: the same turn can be charged again.
    assert http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json').status_code == 201


@pytest.mark.parametrize('role', [Membership.Role.COORDINATOR, Membership.Role.STAFF])
def test_front_desk_charges_but_cannot_void(django_user_model, studio, teacher, ada, role):
    caller = member(django_user_model, studio, role, 'x@example.com')[0]
    appointment = turn(studio, teacher, [ada])
    http = api(caller, studio)
    paid = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    res = http.post(void_url(CashEntry.objects.get(pk=paid.data['id'])), {'reason': 'x'}, format='json')

    assert paid.status_code == 201
    assert (res.status_code, res.data['code']) == (403, 'admin_required')


def test_a_void_needs_a_reason(owner, studio, teacher, ada):
    entry = CashEntry.objects.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-09-30', concept='x'
    )

    res = api(owner, studio).post(void_url(entry), {'reason': '  '}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'blank')
    entry.refresh_from_db()
    assert entry.voided_at is None


def test_a_payment_is_voided_once(owner, studio):
    entry = CashEntry.objects.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-09-30', concept='x',
        voided_at=timezone.now(),
    )

    res = api(owner, studio).post(void_url(entry), {'reason': 'x'}, format='json')

    assert (res.status_code, res.data['code']) == (409, 'already_voided')


def test_another_tenants_payment_cannot_be_voided(django_user_model, studio, gym):
    entry = CashEntry.objects.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-09-30', concept='x'
    )
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    assert api(outsider, gym).post(void_url(entry), {'reason': 'x'}, format='json').status_code == 404


# -- Charging plan periods -----------------------------------------------------


@pytest.fixture
def pilates(studio):
    return Plan.objects.create(tenant=studio, name='Pilates 8', price=250000, sessions_per_period=8)


def test_charging_periods_pays_the_oldest_owed_first_at_each_price(staff, studio, pilates, ada):
    today = local_today(studio)
    start = add_months(today, -1)
    Subscription.objects.create(
        tenant=studio, client=ada, plan=pilates, start_date=start, price_override=200000
    )

    res = api(staff, studio).post(
        client_url(ada, 'charge-periods'), {'count': 3, 'payment_method': 'card'}, format='json'
    )

    assert res.status_code == 201
    # Two owed (last month's and this month's), then one in advance.
    assert [e['period'] for e in res.data] == [
        start.isoformat(), add_months(start, 1).isoformat(), add_months(start, 2).isoformat(),
    ]
    assert {e['amount'] for e in res.data} == {200000}
    assert {e['payment_method'] for e in res.data} == {'card'}
    assert {e['client'] for e in res.data} == {ada.pk}


def test_the_next_charge_skips_what_is_paid(owner, studio, pilates, ada):
    start = add_months(local_today(studio), -2)
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date=start)
    http = api(owner, studio)
    http.post(client_url(ada, 'charge-periods'), {}, format='json')

    res = http.post(client_url(ada, 'charge-periods'), {}, format='json')

    assert [e['period'] for e in res.data] == [add_months(start, 1).isoformat()]


def test_a_voided_period_is_owed_again(owner, studio, pilates, ada):
    start = add_months(local_today(studio), -1)
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date=start)
    http = api(owner, studio)
    first = http.post(client_url(ada, 'charge-periods'), {}, format='json').data[0]
    http.post(void_url(CashEntry.objects.get(pk=first['id'])), {'reason': 'x'}, format='json')

    res = http.post(client_url(ada, 'charge-periods'), {}, format='json')

    assert res.data[0]['period'] == start.isoformat()


def test_an_ended_subscription_has_only_so_many_periods(owner, studio, pilates, ada):
    Subscription.objects.create(
        tenant=studio, client=ada, plan=pilates, start_date='2026-01-14', end_date='2026-03-13'
    )

    res = api(owner, studio).post(client_url(ada, 'charge-periods'), {'count': 3}, format='json')

    assert res.status_code == 400
    assert res.data['code'] == 'not_enough_periods'
    assert not CashEntry.objects.exists()


def test_a_client_without_a_plan_has_no_periods_to_charge(owner, studio, ada):
    res = api(owner, studio).post(client_url(ada, 'charge-periods'), {}, format='json')

    assert res.status_code == 400
    assert res.data['code'] == 'not_enough_periods'


def test_another_tenants_client_cannot_be_charged_periods(django_user_model, studio, gym, pilates, ada):
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    res = api(outsider, gym).post(client_url(ada, 'charge-periods'), {}, format='json')

    assert res.status_code == 404


# -- The client file -----------------------------------------------------------


def test_the_client_file_summarises_plan_and_owed_periods(owner, studio, pilates, ada):
    today = local_today(studio)
    start = add_months(today, -1)
    sub = Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date=start)

    summary = api(owner, studio).get(client_url(ada)).data['billing_summary']

    assert summary['subscription']['id'] == sub.pk
    assert summary['subscription']['plan_name'] == 'Pilates 8'
    assert summary['subscription']['price'] == 250000
    assert summary['subscription']['current_period']['start'] == add_months(start, 1).isoformat()
    assert summary['subscription']['current_period']['end'] == (
        add_months(start, 2) - timedelta(days=1)
    ).isoformat()
    assert summary['subscription']['sessions_total'] == 8
    assert [p['start'] for p in summary['owed_periods']] == [
        start.isoformat(), add_months(start, 1).isoformat(),
    ]
    assert summary['owed_periods'][0]['overdue'] is True
    assert summary['unpaid_turns'] == []
    assert summary['total'] == 500000


def test_a_client_without_a_plan_owes_nothing(owner, studio, ada):
    summary = api(owner, studio).get(client_url(ada)).data['billing_summary']

    assert summary == {'subscription': None, 'owed_periods': [], 'unpaid_turns': [], 'total': 0}


def test_the_payment_history_keeps_voided_payments(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])
    http = api(owner, studio)
    paid = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json').data
    http.post(void_url(CashEntry.objects.get(pk=paid['id'])), {'reason': 'x'}, format='json')
    http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    res = http.get(client_url(ada, 'payments'))

    assert res.status_code == 200
    assert sorted(e['voided_at'] is None for e in res.data['results']) == [False, True]


def test_another_tenants_payment_history_is_not_reachable(django_user_model, studio, gym, ada):
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    assert api(outsider, gym).get(client_url(ada, 'payments')).status_code == 404


def test_deleting_a_client_keeps_the_money_they_paid(owner, studio, pilates, ada):
    """The cash book records what the shop took; erasing the payer does not un-take it."""
    Subscription.objects.create(tenant=studio, client=ada, plan=pilates, start_date='2026-01-14')
    http = api(owner, studio)
    http.post(client_url(ada, 'charge-periods'), {}, format='json')

    res = http.delete(client_url(ada))

    assert res.status_code == 204
    entry = CashEntry.objects.get()
    assert entry.amount == 250000
    assert entry.client is None


# -- Receivables ---------------------------------------------------------------


def test_receivables_answer_in_their_final_shape(staff, studio):
    res = api(staff, studio).get(RECEIVABLES)

    assert res.status_code == 200
    assert res.data == {'total': 0, 'rows': [], 'due_soon': []}


# -- Plan states ---------------------------------------------------------------


def at(tenant, day, hour=10):
    """A moment on a day of the BUSINESS's calendar."""
    return datetime.combine(day, time(hour), tzinfo=ZoneInfo(tenant.timezone))


def subscribe(client, plan, start, **fields):
    return Subscription.objects.create(
        tenant=client.tenant, client=client, plan=plan, start_date=start, **fields
    )


def pay_owed(http, client):
    """Settle every started period through the API, so a plan state shows."""
    owed = http.get(client_url(client)).data['billing_summary']['owed_periods']
    if owed:
        http.post(client_url(client, 'charge-periods'), {'count': len(owed)}, format='json')


def test_a_subscribed_attendee_inside_the_quota_is_covered(owner, studio, teacher, pilates, ada):
    today = local_today(studio)
    subscribe(ada, pilates, today)
    appointment = turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))

    assert billing_of(api(owner, studio), appointment)['Ada'] == {
        'state': 'covered', 'amount': None, 'owed_periods': [],
        'quota_used': 1, 'quota_total': 8,
        'payment_method': None, 'cash_entry': None,
    }


def test_a_covered_attendee_has_nothing_to_charge(owner, studio, teacher, pilates, ada):
    today = local_today(studio)
    subscribe(ada, pilates, today)
    appointment = turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))

    res = api(owner, studio).post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    assert (res.status_code, res.data['code']) == (400, 'covered_by_plan')
    assert not CashEntry.objects.exists()


def test_an_unlimited_plan_covers_without_a_total(owner, studio, teacher, ada):
    libre = Plan.objects.create(tenant=studio, name='Libre', price=300000)
    subscribe(ada, libre, local_today(studio))
    appointment = turn(studio, teacher, [ada], start=at(studio, local_today(studio) + timedelta(days=1)))

    billing = billing_of(api(owner, studio), appointment)['Ada']
    assert (billing['state'], billing['quota_used'], billing['quota_total']) == ('covered', 1, None)


def test_a_client_who_owes_a_past_period_shows_it_on_any_turn(owner, studio, teacher, pilates, ada):
    """Decided against today, not the turn's period: last month's debt shows on
    this month's turn. Only the overdue period is charged -- the current one is
    still inside its grace days."""
    today = local_today(studio)
    last_month = add_months(today, -1)
    subscribe(ada, pilates, last_month)
    appointment = turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))

    billing = billing_of(api(owner, studio), appointment)['Ada']

    assert billing['state'] == 'plan_owed'
    assert billing['amount'] == 250000
    assert [p['start'] for p in billing['owed_periods']] == [last_month.isoformat()]
    assert billing['owed_periods'][0]['overdue'] is True


def test_a_paid_turn_wins_over_a_plan_debt(owner, studio, teacher, pilates, ada):
    subscribe(ada, pilates, add_months(local_today(studio), -1))
    appointment = turn(studio, teacher, [ada])
    http = api(owner, studio)

    res = http.post(charge_url(appointment), {'client': str(ada.pk), 'amount': 80000}, format='json')

    assert res.status_code == 201
    assert billing_of(http, appointment)['Ada']['state'] == 'paid'


def test_voiding_a_period_payment_brings_the_debt_back(owner, studio, teacher, pilates, ada):
    subscribe(ada, pilates, add_months(local_today(studio), -1))
    appointment = turn(studio, teacher, [ada], start=at(studio, local_today(studio) + timedelta(days=1)))
    http = api(owner, studio)
    paid = http.post(client_url(ada, 'charge-periods'), {}, format='json').data[0]
    assert billing_of(http, appointment)['Ada']['state'] == 'covered'

    http.post(void_url(CashEntry.objects.get(pk=paid['id'])), {'reason': 'x'}, format='json')

    assert billing_of(http, appointment)['Ada']['state'] == 'plan_owed'


def test_the_quota_goes_by_start_time_counting_no_shows_but_not_cancellations(
    owner, studio, teacher, ada
):
    duo = Plan.objects.create(tenant=studio, name='Duo', price=100000, sessions_per_period=2)
    today = local_today(studio)
    subscribe(ada, duo, today)
    tomorrow = today + timedelta(days=1)
    # Booked out of order on purpose: the quota follows the diary, not the
    # order the bookings were made in.
    fourth = turn(studio, teacher, [ada], start=at(studio, tomorrow, 13))
    no_show = turn(studio, teacher, [ada], start=at(studio, tomorrow, 10))
    cancelled = turn(studio, teacher, [ada], start=at(studio, tomorrow, 11))
    third = turn(studio, teacher, [ada], start=at(studio, tomorrow, 12))
    AppointmentClient.objects.filter(appointment=no_show).update(attendance='no_show')
    Appointment.objects.filter(pk=cancelled.pk).update(status='cancelled')
    http = api(owner, studio)

    def state(appointment):
        billing = billing_of(http, appointment)['Ada']
        return billing['state'], billing['amount'], billing['quota_used'], billing['quota_total']

    assert state(no_show) == ('covered', None, 1, 2)
    assert state(third) == ('covered', None, 2, 2)
    assert state(fourth) == ('extra', 80000, 3, 2)


def test_a_plan_covers_only_its_categories(owner, studio, teacher, ada):
    pilates_class = Category.objects.create(tenant=studio, name='Pilates')
    massage = Category.objects.create(tenant=studio, name='Masajes')
    plan = Plan.objects.create(tenant=studio, name='Solo pilates', price=200000)
    plan.categories.add(pilates_class)
    subscribe(ada, plan, local_today(studio))
    tomorrow = local_today(studio) + timedelta(days=1)
    reformer = turn(studio, teacher, [ada], start=at(studio, tomorrow, 9), category=pilates_class)
    priced = turn(studio, teacher, [ada], start=at(studio, tomorrow, 10), category=massage)
    unpriced = turn(studio, teacher, [ada], price=None, start=at(studio, tomorrow, 11), category=massage)
    http = api(owner, studio)

    assert billing_of(http, reformer)['Ada']['state'] == 'covered'
    assert billing_of(http, priced)['Ada']['state'] == 'extra'
    assert billing_of(http, priced)['Ada']['amount'] == 80000
    assert billing_of(http, unpriced)['Ada']['state'] == 'no_price'


def test_a_turn_outside_the_subscription_is_charged_per_turn(owner, studio, teacher, pilates, ada):
    today = local_today(studio)
    subscribe(ada, pilates, today + timedelta(days=5))
    before = turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))

    assert billing_of(api(owner, studio), before)['Ada']['state'] == 'charge'


def test_the_quota_is_counted_per_anchored_period_not_per_calendar_month(
    owner, studio, teacher, ada
):
    """Billed on the 14th: the 13th and the 14th of March are two periods, so
    one class a period covers both, while the 2nd and the 13th share one."""
    single = Plan.objects.create(tenant=studio, name='Una', price=100000, sessions_per_period=1)
    subscribe(ada, single, date(2026, 1, 14), end_date=date(2026, 4, 13))
    http = api(owner, studio)
    pay_owed(http, ada)
    early = turn(studio, teacher, [ada], start=at(studio, date(2026, 3, 2)))
    last_day = turn(studio, teacher, [ada], start=at(studio, date(2026, 3, 13)))
    next_period = turn(studio, teacher, [ada], start=at(studio, date(2026, 3, 14)))

    assert billing_of(http, early)['Ada']['state'] == 'covered'
    assert billing_of(http, last_day)['Ada']['state'] == 'extra'
    assert billing_of(http, next_period)['Ada']['state'] == 'covered'
    assert billing_of(http, next_period)['Ada']['quota_used'] == 1


# -- Periods -------------------------------------------------------------------


def test_an_anchor_of_31_lands_on_the_last_day_of_short_months_and_comes_back(
    owner, studio, pilates, ada
):
    subscribe(ada, pilates, date(2026, 1, 31), end_date=date(2026, 5, 30))

    owed = api(owner, studio).get(client_url(ada)).data['billing_summary']['owed_periods']

    assert [(p['start'], p['end'], p['name']) for p in owed] == [
        ('2026-01-31', '2026-02-27', 'enero'),
        ('2026-02-28', '2026-03-30', 'febrero'),
        ('2026-03-31', '2026-04-29', 'marzo'),
        ('2026-04-30', '2026-05-30', 'abril'),
    ]


@pytest.mark.parametrize('rule', ['month_start', 'join_day'])
def test_a_subscription_runs_its_periods_from_the_rules_start_date(owner, studio, pilates, ada, rule):
    """Either rule becomes the anchor of the periods."""
    studio.plan_period_start = rule
    studio.save()
    http = api(owner, studio)
    today = local_today(studio)

    http.post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk}, format='json')
    current = http.get(client_url(ada)).data['billing_summary']['subscription']['current_period']

    first = today if rule == 'join_day' or today.day == 1 else add_months(today.replace(day=1), 1)
    assert current['start'] == first.isoformat()
    assert current['end'] == (add_months(first, 1) - timedelta(days=1)).isoformat()


def test_a_given_start_date_overrides_the_rule(owner, studio, pilates, ada):
    """The rule is only the prefill: one subscription keeps its own anchor."""
    http = api(owner, studio)
    start = local_today(studio) - timedelta(days=3)

    http.post(SUBSCRIPTIONS, {'client': ada.pk, 'plan': pilates.pk, 'start_date': start.isoformat()}, format='json')

    current = http.get(client_url(ada)).data['billing_summary']['subscription']['current_period']
    assert current['start'] == start.isoformat()


@pytest.mark.parametrize('grace, days_ago, overdue', [
    (9, 8, False),   # the day before the due date
    (9, 9, False),   # on the due date: still on time
    (9, 10, True),   # the day after
    (0, 0, False),   # no grace: due the day it starts
    (0, 1, True),
])
def test_a_period_is_overdue_the_day_after_its_due_date(
    owner, studio, teacher, pilates, ada, grace, days_ago, overdue
):
    studio.plan_grace_days = grace
    studio.save()
    start = local_today(studio) - timedelta(days=days_ago)
    subscribe(ada, pilates, start)
    appointment = turn(studio, teacher, [ada], start=at(studio, local_today(studio) + timedelta(days=1)))
    http = api(owner, studio)

    (period,) = http.get(client_url(ada)).data['billing_summary']['owed_periods']

    assert period['due_date'] == (start + timedelta(days=grace)).isoformat()
    assert period['overdue'] is overdue
    assert billing_of(http, appointment)['Ada']['state'] == ('plan_owed' if overdue else 'covered')


# -- The client file, with rules -----------------------------------------------


def test_the_client_file_counts_the_sessions_booked_in_the_period(owner, studio, teacher, pilates, ada):
    today = local_today(studio)
    subscribe(ada, pilates, today)
    for hour in (9, 10, 11):
        turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1), hour))

    current = api(owner, studio).get(client_url(ada)).data['billing_summary']['subscription']

    assert (current['sessions_used'], current['sessions_total'], current['sessions_left']) == (3, 8, 5)


def test_sessions_left_is_what_can_still_be_booked(owner, studio, teacher, pilates, ada):
    """Quedan N = quota - (turns already had + turns booked ahead) in the
    period: a no-show still spent its session, a cancelled one gave it back."""
    today = local_today(studio)
    subscribe(ada, pilates, today - timedelta(days=5))
    turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=3)))  # attended
    no_show = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=2)))
    AppointmentClient.objects.filter(appointment=no_show).update(attendance='no_show')
    cancelled = turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))
    Appointment.objects.filter(pk=cancelled.pk).update(status='cancelled')
    turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=2)))  # booked ahead

    current = api(owner, studio).get(client_url(ada)).data['billing_summary']['subscription']

    assert (current['sessions_used'], current['booked_ahead'], current['sessions_left']) == (3, 1, 5)


@pytest.fixture
def live_since_last_week(settings, studio):
    settings.BILLING_GO_LIVE = local_today(studio) - timedelta(days=7)
    return settings.BILLING_GO_LIVE


def test_unpaid_past_turns_since_go_live_are_owed(owner, studio, teacher, ada, live_since_last_week):
    today = local_today(studio)
    owed = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=2)))
    paid = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=3)))
    turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=8)))  # before go-live
    turn(studio, teacher, [ada], start=at(studio, today + timedelta(days=1)))  # not happened yet
    cancelled = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=4)))
    Appointment.objects.filter(pk=cancelled.pk).update(status='cancelled')
    http = api(owner, studio)
    http.post(charge_url(paid), {'client': str(ada.pk)}, format='json')

    summary = http.get(client_url(ada)).data['billing_summary']

    assert [t['appointment'] for t in summary['unpaid_turns']] == [str(owed.pk)]
    assert summary['unpaid_turns'][0]['amount'] == 80000
    assert summary['total'] == 80000


def test_a_turn_attended_while_owing_is_settled_by_paying_the_month(
    owner, studio, teacher, pilates, ada, live_since_last_week
):
    """Covered by the period it falls in: the plan debt is what is owed, not
    the turn, so paying the month leaves nothing else."""
    subscribe(ada, pilates, add_months(local_today(studio), -1))
    turn(studio, teacher, [ada], start=at(studio, local_today(studio) - timedelta(days=1)))
    http = api(owner, studio)

    assert http.get(client_url(ada)).data['billing_summary']['unpaid_turns'] == []

    pay_owed(http, ada)
    assert http.get(client_url(ada)).data['billing_summary']['total'] == 0


# -- Receivables ---------------------------------------------------------------


def test_receivables_list_who_owes_oldest_debt_first(
    staff, studio, teacher, pilates, ada, bob, live_since_last_week
):
    today = local_today(studio)
    two_months_ago = add_months(today, -2)
    # Ada's debt is one turn from yesterday; Bob's plan has been overdue for
    # two months, so he leads although the alphabet says otherwise.
    turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=1)))
    subscribe(bob, pilates, two_months_ago)
    Client.objects.create(tenant=studio, name='Carla')  # owes nothing

    res = api(staff, studio).get(RECEIVABLES)

    bob_row, ada_row = res.data['rows']
    assert bob_row['client_name'] == 'Bob'
    assert bob_row['client_phone'] == ''
    assert bob_row['oldest_debt'] == two_months_ago.isoformat()
    assert [p['start'] for p in bob_row['overdue_periods']] == [
        two_months_ago.isoformat(), add_months(two_months_ago, 1).isoformat(),
    ]
    assert bob_row['amount'] == 500000
    assert bob_row['subscription'] is not None
    assert ada_row['client_name'] == 'Ada'
    assert ada_row['client_phone'] == '+595981123456'
    assert ada_row['subscription'] is None
    assert ada_row['unpaid_turns'][0]['amount'] == 80000
    assert ada_row['oldest_debt'] == (today - timedelta(days=1)).isoformat()
    assert res.data['total'] == 580000


@pytest.mark.parametrize('due_in, listed', [(0, True), (3, True), (4, False)])
def test_due_soon_is_the_current_period_due_within_three_days(
    staff, studio, pilates, ada, due_in, listed
):
    """Not overdue yet, so in neither the rows nor the total."""
    subscribe(ada, pilates, local_today(studio) + timedelta(days=due_in - 9))

    res = api(staff, studio).get(RECEIVABLES)

    assert res.data['rows'] == []
    assert res.data['total'] == 0
    assert [row['client_name'] for row in res.data['due_soon']] == (['Ada'] if listed else [])


def test_a_paid_current_period_is_not_due_soon(owner, studio, pilates, ada):
    subscribe(ada, pilates, local_today(studio) - timedelta(days=8))
    http = api(owner, studio)
    pay_owed(http, ada)

    assert http.get(RECEIVABLES).data['due_soon'] == []


def test_turns_before_go_live_are_never_receivable(staff, studio, teacher, ada, settings):
    settings.BILLING_GO_LIVE = local_today(studio)
    turn(studio, teacher, [ada], start=at(studio, local_today(studio) - timedelta(days=1)))

    assert api(staff, studio).get(RECEIVABLES).data['rows'] == []


def test_receivables_are_isolated_per_tenant(django_user_model, studio, gym, pilates, ada):
    subscribe(ada, pilates, add_months(local_today(studio), -2))
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    assert api(outsider, gym).get(RECEIVABLES).data['rows'] == []


# -- Cobrar todo ---------------------------------------------------------------


def test_charge_all_settles_owed_periods_and_unpaid_turns_at_once(
    staff, studio, teacher, pilates, ada, live_since_last_week
):
    today = local_today(studio)
    subscribe(ada, pilates, add_months(today, -1), price_override=200000)
    # A turn the plan does not cover: its category is not the plan's.
    pilates.categories.add(Category.objects.create(tenant=studio, name='Pilates'))
    massage = Category.objects.create(tenant=studio, name='Masajes')
    extra = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=1)), category=massage)
    http = api(staff, studio)

    res = http.post(client_url(ada, 'charge-all'), {'payment_method': 'transfer'}, format='json')

    assert res.status_code == 201
    # Last month's and this month's period, then the turn.
    assert res.data['total'] == 200000 + 200000 + 80000
    assert [(e['period'], e['appointment']) for e in res.data['entries']] == [
        (add_months(today, -1).isoformat(), None),
        (add_months(add_months(today, -1), 1).isoformat(), None),
        (None, extra.pk),
    ]
    assert {e['payment_method'] for e in res.data['entries']} == {'transfer'}
    assert http.get(client_url(ada)).data['billing_summary']['total'] == 0


def test_charge_all_twice_charges_once(owner, studio, pilates, ada):
    subscribe(ada, pilates, local_today(studio))
    http = api(owner, studio)

    first = http.post(client_url(ada, 'charge-all'), {}, format='json')
    second = http.post(client_url(ada, 'charge-all'), {}, format='json')

    assert first.status_code == 201
    assert (second.status_code, second.data['code']) == (409, 'nothing_to_charge')
    assert CashEntry.objects.count() == 1


def test_charge_all_files_everything_or_nothing(
    monkeypatch, owner, studio, teacher, pilates, ada, live_since_last_week
):
    """Another receptionist charges the turn between the read and the write:
    the whole charge is refused, this month's period included."""
    from apps.scheduling import billing

    today = local_today(studio)
    subscribe(ada, pilates, today)
    # The day before the plan starts, so the turn is charged on its own.
    before = turn(studio, teacher, [ada], start=at(studio, today - timedelta(days=1)))
    read = billing.billing_summary

    def raced(client):
        summary = read(client)
        CashEntry.objects.create(
            tenant=studio, kind='income', amount=1, occurred_on=today, concept='x',
            appointment=before, client=ada,
        )
        return summary

    monkeypatch.setattr(billing, 'billing_summary', raced)

    res = api(owner, studio).post(client_url(ada, 'charge-all'), {}, format='json')

    assert (res.status_code, res.data['code']) == (409, 'already_paid')
    assert not CashEntry.objects.filter(period__isnull=False).exists()


def test_charge_all_with_nothing_owed(owner, studio, ada):
    res = api(owner, studio).post(client_url(ada, 'charge-all'), {}, format='json')

    assert (res.status_code, res.data['code']) == (409, 'nothing_to_charge')


def test_another_tenants_client_cannot_be_charged_all(django_user_model, studio, gym, pilates, ada):
    subscribe(ada, pilates, local_today(studio))
    outsider = member(django_user_model, gym, Membership.Role.OWNER, 'g@example.com')[0]

    assert api(outsider, gym).post(client_url(ada, 'charge-all'), {}, format='json').status_code == 404
    assert not CashEntry.objects.exists()
