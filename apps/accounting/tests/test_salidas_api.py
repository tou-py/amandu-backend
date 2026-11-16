from datetime import date

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import Caja, CashEntry, MethodCaja
from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

LIST_URL = reverse('accounting:cashentry-list')
SUMMARY_URL = reverse('accounting:cashentry-summary')
OPENED = date(2026, 1, 5)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY', addon_cobros=True, addon_caja=True)


@pytest.fixture
def drawer(salon):
    caja = Caja.objects.create(tenant=salon, name='Principal', opened_on=OPENED, opening_balance=0)
    for method in CashEntry.PaymentMethod.values:
        MethodCaja.objects.create(tenant=salon, payment_method=method, caja=caja)
    return caja


def member(django_user_model, tenant, role, email=None):
    user = django_user_model.objects.create_user(email=email or f'{role}-{tenant.slug}@example.com', password='pw')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def admin(django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.ADMIN)


@pytest.fixture
def desk(django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.COORDINATOR)


def salida(caja, **changes):
    return {
        'kind': 'expense', 'amount': 45000, 'occurred_on': '2026-03-02', 'concept': 'Shampoo',
        'payment_method': 'cash', 'category': 'supplies', 'caja': caja.pk, **changes,
    }


def test_a_salida_is_recorded_with_its_caja_and_category(desk, drawer):
    res = desk.post(LIST_URL, salida(drawer), format='json')
    assert res.status_code == 201
    created = CashEntry.objects.get(pk=res.data['id'])
    assert (created.kind, created.amount, created.category, created.caja) == ('expense', 45000, 'supplies', drawer)
    assert res.data['caja_name'] == 'Principal'


@pytest.mark.parametrize('field', ('category', 'caja', 'payment_method'))
def test_a_salida_cannot_leave_out(desk, drawer, field):
    body = salida(drawer)
    del body[field]
    assert field in desk.post(LIST_URL, body, format='json').data


def test_a_salida_cannot_be_dated_before_its_caja_opened(desk, drawer):
    res = desk.post(LIST_URL, salida(drawer, occurred_on='2026-01-04'), format='json')
    assert (res.status_code, res.data['code']) == (400, 'before_caja_opened')
    assert desk.post(LIST_URL, salida(drawer, occurred_on='2026-01-05'), format='json').status_code == 201


def test_another_business_caja_is_refused(desk, drawer, salon):
    other = Tenant.objects.create(name='Clinic', slug='clinic', country='PY', addon_cobros=True, addon_caja=True)
    foreign = Caja.objects.create(tenant=other, name='Suya', opened_on=OPENED, opening_balance=0)
    assert 'caja' in desk.post(LIST_URL, salida(foreign), format='json').data


def test_an_archived_caja_is_refused(desk, drawer, salon):
    gone = Caja.objects.create(tenant=salon, name='Vieja', opened_on=OPENED, opening_balance=0, archived=True)
    assert 'caja' in desk.post(LIST_URL, salida(gone), format='json').data


def test_without_caja_a_salida_is_refused_even_with_cobros(desk, drawer, salon):
    Tenant.objects.filter(pk=salon.pk).update(addon_caja=False)
    res = desk.post(LIST_URL, salida(drawer), format='json')
    assert (res.status_code, res.data['code']) == (403, 'addon_required')
    assert desk.get(LIST_URL, {'kind': 'expense'}).status_code == 403
    assert desk.get(LIST_URL, {'kind': 'all'}).status_code == 403


def test_income_takes_no_category(admin, drawer):
    res = admin.post(LIST_URL, salida(drawer, kind='income'), format='json')
    assert 'category' in res.data


def test_income_with_no_caja_lands_where_its_method_maps(admin, drawer):
    body = salida(drawer, kind='income', category='')
    del body['caja']
    res = admin.post(LIST_URL, body, format='json')
    assert res.data['caja'] == drawer.pk


def test_the_database_holds_a_salida_complete(salon, drawer):
    for broken in ({'category': ''}, {'caja': None}):
        fields = {'category': 'rent', 'caja': drawer, **broken}
        with pytest.raises(IntegrityError), transaction.atomic():
            CashEntry.objects.create(tenant=salon, kind='expense', amount=1, occurred_on=OPENED, concept='x', **fields)
    with pytest.raises(IntegrityError), transaction.atomic():
        CashEntry.objects.create(
            tenant=salon, kind='income', amount=1, occurred_on=OPENED, concept='x', category='rent'
        )


def test_an_old_client_never_reads_a_salida_as_a_payment(admin, drawer):
    admin.post(LIST_URL, salida(drawer), format='json')
    assert admin.get(LIST_URL).data['results'] == []
    assert len(admin.get(LIST_URL, {'kind': 'expense'}).data['results']) == 1
    assert len(admin.get(LIST_URL, {'kind': 'all'}).data['results']) == 1


def test_coordinators_read_only_their_own_salidas(admin, desk, django_user_model, salon, drawer):
    other = member(django_user_model, salon, Membership.Role.COORDINATOR, email='other@example.com')
    desk.post(LIST_URL, salida(drawer, concept='Mine'), format='json')
    other.post(LIST_URL, salida(drawer, concept='Theirs'), format='json')
    assert [row['concept'] for row in desk.get(LIST_URL, {'kind': 'expense'}).data['results']] == ['Mine']
    assert [row['concept'] for row in desk.get(LIST_URL, {'kind': 'all'}).data['results']] == ['Mine']
    assert len(admin.get(LIST_URL, {'kind': 'expense'}).data['results']) == 2


def test_staff_record_and_read_no_salidas(django_user_model, salon, drawer):
    staff = member(django_user_model, salon, Membership.Role.STAFF)
    assert staff.post(LIST_URL, salida(drawer), format='json').status_code == 403
    assert staff.get(LIST_URL, {'kind': 'expense'}).status_code == 403


def test_the_summary_gives_salidas_and_the_diferencia_to_owner_and_admin_only(admin, desk, drawer):
    admin.post(LIST_URL, salida(drawer, kind='income', category=''), format='json')
    admin.post(LIST_URL, salida(drawer, amount=1000, category='rent'), format='json')
    admin.post(LIST_URL, salida(drawer, amount=500, category='rent'), format='json')
    answer = admin.get(SUMMARY_URL).data
    assert (answer['total'], answer['expenses'], answer['difference']) == (45000, 1500, 43500)
    assert answer['by_category'] == [{'category': 'rent', 'total': 1500, 'count': 2}]
    answer = desk.get(SUMMARY_URL).data
    assert answer['total'] == 45000
    assert (answer['expenses'], answer['by_category'], answer['difference']) == (None, None, None)


def test_a_voided_salida_counts_for_nothing_in_the_summary(admin, drawer):
    entry = admin.post(LIST_URL, salida(drawer), format='json').data
    assert admin.post(reverse('accounting:cashentry-void', args=[entry['id']]), {'reason': 'x'}).status_code == 200
    assert admin.get(SUMMARY_URL).data['expenses'] == 0


def test_undoing_a_salida_needs_caja_on(admin, drawer, salon):
    entry = admin.post(LIST_URL, salida(drawer), format='json').data
    Tenant.objects.filter(pk=salon.pk).update(addon_caja=False)
    res = admin.post(reverse('accounting:cashentry-void', args=[entry['id']]), {'reason': 'x'})
    assert (res.status_code, res.data['code']) == (403, 'addon_required')
