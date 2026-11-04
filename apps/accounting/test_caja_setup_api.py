from datetime import date

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import Caja, CashEntry, MethodCaja
from apps.accounts.models import Membership
from apps.scheduling.billing import local_today
from apps.tenancy.models import Tenant

SETUP_URL = reverse('accounting:caja-setup')
ALL_METHODS = CashEntry.PaymentMethod.values


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='PY', addon_cobros=True)


def member(django_user_model, tenant, role):
    user = django_user_model.objects.create_user(email=f'{role}-{tenant.slug}@example.com', password='pw')
    Membership.objects.create(user=user, tenant=tenant, role=role)
    http = APIClient()
    http.force_authenticate(user=user)
    http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def owner(django_user_model, salon):
    return member(django_user_model, salon, Membership.Role.OWNER)


def setup_body(**changes):
    return {'cajas': [{'name': 'Principal', 'opening_balance': 500, 'methods': ALL_METHODS, **changes}]}


def fresh(tenant):
    return Tenant.objects.get(pk=tenant.pk)


def test_the_setup_turns_caja_on_with_everything_at_once(owner, salon):
    res = owner.post(SETUP_URL, {'cajas': [
        {'name': 'Principal', 'opening_balance': 500, 'methods': ['cash', 'other']},
        {'name': 'Banco', 'opening_balance': 0, 'methods': ['card', 'transfer', 'qr']},
    ]}, format='json')
    assert res.status_code == 201
    assert fresh(salon).addon_caja
    assert [(row['name'], row['saldo'], row['opened_on']) for row in res.data] == [
        ('Principal', 500, local_today(salon).isoformat()),
        ('Banco', 0, local_today(salon).isoformat()),
    ]
    assert MethodCaja.objects.get(tenant=salon, payment_method='qr').caja.name == 'Banco'


@pytest.mark.parametrize('methods', (['cash'], [*ALL_METHODS, 'cash']))
def test_the_setup_needs_each_method_exactly_once(owner, salon, methods):
    res = owner.post(SETUP_URL, setup_body(methods=methods), format='json')
    assert res.status_code == 400
    assert not fresh(salon).addon_caja
    assert not Caja.objects.exists()


def test_the_setup_is_for_the_owner_and_needs_cobros(owner, django_user_model, salon):
    admin = member(django_user_model, salon, Membership.Role.ADMIN)
    assert admin.post(SETUP_URL, setup_body(), format='json').status_code == 403
    Tenant.objects.filter(pk=salon.pk).update(addon_cobros=False)
    res = owner.post(SETUP_URL, setup_body(), format='json')
    assert (res.status_code, res.data['code']) == (409, 'cobros_required')


def test_dropping_cobros_drops_caja_and_the_add_on_list_leaves_it_alone(owner, salon):
    owner.post(SETUP_URL, setup_body(), format='json')
    subscription = '/api/tenant/subscription/'
    res = owner.patch(subscription, {'addons': ['cobros', 'reservas']}, format='json')
    assert res.status_code == 200
    assert fresh(salon).addon_caja
    owner.patch(subscription, {'addons': ['reservas']}, format='json')
    assert not fresh(salon).addon_caja


def test_turning_it_back_on_counts_every_caja_again(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    caja = Caja.objects.get(pk=first['id'])
    CashEntry.objects.create(
        tenant=salon, caja=caja, kind='income', amount=70, occurred_on=date(2026, 1, 5), concept='old',
    )
    Caja.objects.filter(pk=caja.pk).update(opened_on=date(2026, 1, 5))
    assert owner.delete(SETUP_URL).status_code == 204
    assert owner.get(SETUP_URL).data == [{'id': caja.pk, 'name': 'Principal'}]

    res = owner.post(SETUP_URL, setup_body(), format='json')
    assert (res.status_code, res.data['code']) == (400, 'caja_not_counted')

    res = owner.post(SETUP_URL, setup_body(id=caja.pk, opening_balance=900), format='json')
    assert res.status_code == 201
    assert res.data[0]['saldo'] == 900  # What was counted, not 900 + the old 70.
    assert Caja.objects.count() == 1


def test_a_caja_that_moved_today_cannot_be_counted_again_today(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    CashEntry.objects.create(
        tenant=salon, caja_id=first['id'], kind='income', amount=70, occurred_on=local_today(salon), concept='x',
    )
    owner.delete(SETUP_URL)
    res = owner.post(SETUP_URL, setup_body(id=first['id']), format='json')
    assert (res.status_code, res.data['code']) == (409, 'caja_moved_today')
    assert not fresh(salon).addon_caja
