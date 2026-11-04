from datetime import date

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import CashEntry
from apps.accounts.models import Membership
from apps.scheduling.models import Client
from apps.tenancy.models import Tenant

LIST_URL = reverse('accounting:cashentry-list')
SUMMARY_URL = reverse('accounting:cashentry-summary')


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY', addon_cobros=True)


def member(django_user_model, tenant, role):
    user = django_user_model.objects.create_user(email=f'{role}-{tenant.slug}@example.com', password='pw')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    return user


def api(user, tenant):
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def desk(django_user_model, salon):
    return api(member(django_user_model, salon, Membership.Role.COORDINATOR), salon)


def entry(tenant, amount, method=CashEntry.PaymentMethod.CASH, day=date(2026, 11, 2), **extra):
    return CashEntry.objects.create(
        tenant=tenant, kind=CashEntry.Kind.INCOME, amount=amount, occurred_on=day,
        concept='Turno', payment_method=method, **extra,
    )


def test_the_front_desk_reads_the_book_newest_first(desk, salon):
    entry(salon, 100, day=date(2026, 11, 1))
    entry(salon, 200, day=date(2026, 11, 3))
    res = desk.get(LIST_URL)
    assert res.status_code == 200
    assert [row['amount'] for row in res.data['results']] == [200, 100]


def test_each_row_names_who_paid(desk, salon):
    client = Client.objects.create(tenant=salon, name='Marta')
    entry(salon, 100, client=client)
    entry(salon, 200, day=date(2026, 11, 1))
    names = [row['client_name'] for row in desk.get(LIST_URL).data['results']]
    assert names == ['Marta', None]


def test_filters_by_day_and_method(desk, salon):
    entry(salon, 100, day=date(2026, 11, 1))
    entry(salon, 200, day=date(2026, 11, 2), method=CashEntry.PaymentMethod.TRANSFER)
    entry(salon, 400, day=date(2026, 11, 3), method=CashEntry.PaymentMethod.QR)

    amounts = lambda query: [row['amount'] for row in desk.get(LIST_URL, query).data['results']]  # noqa: E731
    assert amounts({'from': '2026-11-02'}) == [400, 200]
    assert amounts({'to': '2026-11-02'}) == [200, 100]
    assert amounts({'from': '2026-11-02', 'to': '2026-11-02'}) == [200]
    assert amounts({'payment_method': 'qr'}) == [400]


def test_a_bad_filter_is_refused_rather_than_ignored(desk):
    assert desk.get(LIST_URL, {'from': 'ayer'}).status_code == 400
    assert desk.get(LIST_URL, {'payment_method': 'bitcoin'}).status_code == 400
    assert desk.get(LIST_URL, {'from': '2026-11-03', 'to': '2026-11-01'}).status_code == 400


def test_the_summary_totals_per_method(desk, salon):
    entry(salon, 100)
    entry(salon, 300)
    entry(salon, 500, method=CashEntry.PaymentMethod.QR)
    res = desk.get(SUMMARY_URL)
    assert res.status_code == 200
    assert res.data['total'] == 900
    assert res.data['count'] == 3
    assert {row['payment_method']: (row['total'], row['count']) for row in res.data['by_method']} == {
        'qr': (500, 1),
        'cash': (400, 2),
    }


def test_the_summary_answers_the_same_filters_as_the_list(desk, salon):
    entry(salon, 100, day=date(2026, 11, 1))
    entry(salon, 200, day=date(2026, 11, 3))
    assert desk.get(SUMMARY_URL, {'from': '2026-11-02'}).data['total'] == 200


def test_a_void_stays_listed_and_counts_for_nothing(desk, salon):
    from django.utils import timezone

    entry(salon, 100, voided_at=timezone.now(), void_reason='error')
    entry(salon, 200)
    assert len(desk.get(LIST_URL).data['results']) == 2
    summary = desk.get(SUMMARY_URL).data
    assert (summary['total'], summary['count']) == (200, 1)


def test_an_empty_book_adds_up_to_zero(desk):
    assert desk.get(SUMMARY_URL).data == {'total': 0, 'count': 0, 'by_method': []}


def test_expenses_are_not_money_collected(desk, salon):
    CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.EXPENSE, amount=999, occurred_on=date(2026, 11, 2), concept='Luz',
    )
    assert desk.get(LIST_URL).data['results'] == []
    assert desk.get(SUMMARY_URL).data['total'] == 0


def test_one_business_never_reads_another(desk, salon):
    other = Tenant.objects.create(name='Clinic', slug='clinic', country='PY', addon_cobros=True)
    entry(other, 700)
    assert desk.get(LIST_URL).data['results'] == []
    assert desk.get(SUMMARY_URL).data['total'] == 0


def test_staff_do_not_read_the_book(django_user_model, salon):
    http = api(member(django_user_model, salon, Membership.Role.STAFF), salon)
    assert http.get(LIST_URL).status_code == 403
    assert http.get(SUMMARY_URL).status_code == 403


def test_without_the_add_on_nobody_reads_the_book(django_user_model, salon):
    salon.addon_cobros = False
    salon.save(update_fields=['addon_cobros'])
    http = api(member(django_user_model, salon, Membership.Role.OWNER), salon)
    assert http.get(LIST_URL).status_code == 403
    assert http.get(SUMMARY_URL).status_code == 403


def test_a_turn_can_be_recorded_as_paid_by_qr(desk):
    res = desk.post(
        LIST_URL,
        {'kind': 'income', 'amount': 100, 'occurred_on': '2026-11-02', 'concept': 'x', 'payment_method': 'qr'},
        format='json',
    )
    assert res.status_code == 201
    assert res.data['payment_method'] == 'qr'
