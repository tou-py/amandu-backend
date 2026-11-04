from datetime import date

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import Caja, CashEntry
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

    entry(salon, 100, voided_at=timezone.now(), voided_on=date(2026, 11, 2), void_reason='error')
    entry(salon, 200)
    assert len(desk.get(LIST_URL).data['results']) == 2
    summary = desk.get(SUMMARY_URL).data
    assert (summary['total'], summary['count']) == (200, 1)


def test_an_empty_book_adds_up_to_zero(desk):
    assert desk.get(SUMMARY_URL).data == {
        'total': 0, 'count': 0, 'by_method': [], 'expenses': None, 'by_category': None, 'difference': None,
    }


def test_expenses_are_not_money_collected(desk, salon):
    CashEntry.objects.create(
        tenant=salon, kind=CashEntry.Kind.EXPENSE, amount=999, occurred_on=date(2026, 11, 2), concept='Luz',
        category='utilities',
        caja=Caja.objects.create(tenant=salon, name='Principal', opened_on=date(2026, 11, 1), opening_balance=0),
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


def test_an_undo_is_dated_on_the_business_day_it_happened(django_user_model, salon, monkeypatch):
    """Past midnight in UTC, still the evening before on the business's wall."""
    from datetime import datetime, timezone as utc

    from django.utils import timezone

    salon.timezone = 'America/Asuncion'
    salon.save(update_fields=['timezone'])
    paid = entry(salon, 100, day=date(2026, 10, 1))
    monkeypatch.setattr(timezone, 'now', lambda: datetime(2026, 11, 5, 1, 0, tzinfo=utc.utc))

    http = api(member(django_user_model, salon, Membership.Role.OWNER), salon)
    res = http.post(reverse('accounting:cashentry-void', args=[paid.pk]), {'reason': 'error'}, format='json')

    assert res.status_code == 200
    assert res.data['voided_on'] == '2026-11-04'
    # The day it was paid is a fact of its own, and stays.
    assert res.data['occurred_on'] == '2026-10-01'


def test_past_voids_get_the_business_day_they_happened_on(salon):
    """The data half of 0005, on a row from before the day was recorded."""
    import importlib
    from datetime import datetime, timezone as utc

    from django.apps import apps as django_apps
    from django.db import connection

    migration = importlib.import_module('apps.accounting.migrations.0005_cashentry_voided_on')
    salon.timezone = 'America/Asuncion'
    salon.save(update_fields=['timezone'])
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE tb_cash_entry DROP CONSTRAINT cash_entry_void_has_its_day')
    old = entry(salon, 100, voided_at=datetime(2026, 11, 5, 1, 0, tzinfo=utc.utc))
    live = entry(salon, 200)

    migration.date_past_voids(django_apps, None)

    old.refresh_from_db()
    live.refresh_from_db()
    assert old.voided_on == date(2026, 11, 4)
    assert live.voided_on is None


def test_the_database_refuses_a_void_with_no_day(salon):
    from django.db import IntegrityError, transaction
    from django.utils import timezone

    with pytest.raises(IntegrityError), transaction.atomic():
        entry(salon, 100, voided_at=timezone.now())
