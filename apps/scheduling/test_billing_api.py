"""
Charging and voiding, and the billing shapes the front end renders: the
`billing` object on every attendee, the client file's `billing_summary` and
the receivables list. Driven through the HTTP API only.

Phase 0 covers the contract and the simple states (paid, charge, no_price).
The plan states and the receivables rows arrive with phase 1's rules.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.commons.dates import add_months
from apps.scheduling.billing import local_today
from apps.scheduling.models import Appointment, Client, Plan, Service, Subscription
from apps.tenancy.models import Tenant

RECEIVABLES = reverse('scheduling:receivables')
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


def turn(tenant, professional, clients, price=80000, start=YESTERDAY):
    service = Service.objects.create(
        tenant=tenant, name=f'Masaje {start.isoformat()}', price=price,
        duration=timedelta(minutes=60),
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


def test_an_unpriced_turn_needs_an_amount(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada], price=None)
    http = api(owner, studio)

    assert http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json').status_code == 400
    assert http.post(
        charge_url(appointment), {'client': str(ada.pk), 'amount': 50000}, format='json'
    ).status_code == 201


def test_a_paid_attendee_cannot_be_charged_twice(owner, studio, teacher, ada):
    appointment = turn(studio, teacher, [ada])
    http = api(owner, studio)
    http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    res = http.post(charge_url(appointment), {'client': str(ada.pk)}, format='json')

    assert res.status_code == 400
    assert CashEntry.objects.count() == 1


def test_only_someone_on_the_roster_is_charged(owner, studio, teacher, ada, bob):
    appointment = turn(studio, teacher, [ada])

    res = api(owner, studio).post(charge_url(appointment), {'client': str(bob.pk)}, format='json')

    assert res.status_code == 400


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
    assert res.status_code == 403


def test_a_void_needs_a_reason(owner, studio, teacher, ada):
    entry = CashEntry.objects.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-09-30', concept='x'
    )

    res = api(owner, studio).post(void_url(entry), {'reason': '  '}, format='json')

    assert res.status_code == 400
    entry.refresh_from_db()
    assert entry.voided_at is None


def test_a_payment_is_voided_once(owner, studio):
    entry = CashEntry.objects.create(
        tenant=studio, kind='income', amount=1, occurred_on='2026-09-30', concept='x',
        voided_at=timezone.now(),
    )

    assert api(owner, studio).post(void_url(entry), {'reason': 'x'}, format='json').status_code == 400


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
    assert not CashEntry.objects.exists()


def test_a_client_without_a_plan_has_no_periods_to_charge(owner, studio, ada):
    res = api(owner, studio).post(client_url(ada, 'charge-periods'), {}, format='json')

    assert res.status_code == 400


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
