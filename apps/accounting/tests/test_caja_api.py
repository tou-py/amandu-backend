from datetime import date

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounting.models import Caja, CashEntry, MethodCaja
from apps.accounts.models import Membership
from apps.scheduling.billing import local_today
from apps.tenancy.models import Tenant

SETUP_URL = reverse('accounting:caja-setup')
LIST_URL = reverse('accounting:caja-list')
METHODS_URL = reverse('accounting:caja-assign-methods')
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


# --- the cajas once set up ---------------------------------------------------


def test_coordinators_read_names_and_the_map_but_no_figures(owner, django_user_model, salon):
    owner.post(SETUP_URL, setup_body(), format='json')
    for role in (Membership.Role.COORDINATOR, Membership.Role.STAFF):
        row = member(django_user_model, salon, role).get(LIST_URL).data[0]
        assert row['name'] == 'Principal'
        assert sorted(row['methods']) == sorted(ALL_METHODS)
        assert (row['saldo'], row['opening_balance']) == (None, None)
    assert owner.get(LIST_URL).data[0]['saldo'] == 500


def test_only_owner_and_admin_change_cajas(owner, django_user_model, salon):
    owner.post(SETUP_URL, setup_body(), format='json')
    desk = member(django_user_model, salon, Membership.Role.COORDINATOR)
    assert desk.post(LIST_URL, {'name': 'x', 'opening_balance': 0}, format='json').status_code == 403
    admin = member(django_user_model, salon, Membership.Role.ADMIN)
    res = admin.post(LIST_URL, {'name': 'Banco', 'opening_balance': 10}, format='json')
    assert (res.status_code, res.data['saldo']) == (201, 10)
    assert res.data['opened_on'] == local_today(salon).isoformat()


def test_a_caja_is_opened_today_whatever_the_request_says(owner, salon):
    owner.post(SETUP_URL, setup_body(), format='json')
    res = owner.post(LIST_URL, {'name': 'Banco', 'opening_balance': 0, 'opened_on': '2020-01-01'}, format='json')
    assert res.data['opened_on'] == local_today(salon).isoformat()


def test_the_count_stands_once_a_caja_has_movements(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    url = reverse('accounting:caja-detail', args=[first['id']])
    assert owner.patch(url, {'opening_balance': 600}, format='json').data['saldo'] == 600
    CashEntry.objects.create(
        tenant=salon, caja_id=first['id'], kind='income', amount=1, occurred_on=local_today(salon), concept='x',
    )
    res = owner.patch(url, {'opening_balance': 700}, format='json')
    assert (res.status_code, res.data['code']) == (409, 'caja_has_movements')
    assert owner.patch(url, {'name': 'Cajón'}, format='json').status_code == 200


def test_two_live_cajas_cannot_share_a_name(owner):
    owner.post(SETUP_URL, setup_body(), format='json')
    res = owner.post(LIST_URL, {'name': 'Principal', 'opening_balance': 0}, format='json')
    assert (res.status_code, res.data['code']) == (409, 'caja_name_taken')


def test_archiving_needs_no_method_and_no_money(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    url = reverse('accounting:caja-archive', args=[first['id']])
    res = owner.post(url)
    assert (res.status_code, res.data['code']) == (409, 'caja_in_use')

    other = owner.post(LIST_URL, {'name': 'Banco', 'opening_balance': 10}, format='json').data
    res = owner.post(reverse('accounting:caja-archive', args=[other['id']]))
    assert (res.status_code, res.data['code']) == (409, 'caja_not_empty')

    owner.patch(reverse('accounting:caja-detail', args=[other['id']]), {'opening_balance': 0}, format='json')
    assert owner.post(reverse('accounting:caja-archive', args=[other['id']])).status_code == 204
    assert [row['name'] for row in owner.get(LIST_URL).data] == ['Principal']


def test_the_map_is_replaced_whole(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    other = owner.post(LIST_URL, {'name': 'Banco', 'opening_balance': 0}, format='json').data
    partial = [{'payment_method': 'cash', 'caja': other['id']}]
    assert owner.put(METHODS_URL, {'assignments': partial}, format='json').status_code == 400
    assert MethodCaja.objects.filter(tenant=salon, caja_id=first['id']).count() == len(ALL_METHODS)
    whole = [{'payment_method': method, 'caja': other['id']} for method in ALL_METHODS]
    res = owner.put(METHODS_URL, {'assignments': whole}, format='json')
    assert res.status_code == 200
    assert sorted(res.data[1]['methods']) == sorted(ALL_METHODS)


def test_one_business_never_reads_or_uses_another_ones_cajas(owner, django_user_model, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    clinic = Tenant.objects.create(name='Clinic', slug='clinic', country='PY', addon_cobros=True, addon_caja=True)
    theirs = member(django_user_model, clinic, Membership.Role.OWNER)
    assert theirs.get(LIST_URL).data == []
    assert theirs.get(reverse('accounting:caja-detail', args=[first['id']])).status_code == 404
    whole = [{'payment_method': method, 'caja': first['id']} for method in ALL_METHODS]
    assert theirs.put(METHODS_URL, {'assignments': whole}, format='json').status_code == 400


def test_without_caja_the_screens_are_closed(django_user_model, salon):
    assert member(django_user_model, salon, Membership.Role.OWNER).get(LIST_URL).status_code == 403


def test_the_list_shows_the_saldo_of_today_not_of_the_opening(owner, salon):
    first = owner.post(SETUP_URL, setup_body(), format='json').data[0]
    CashEntry.objects.create(
        tenant=salon, caja_id=first['id'], kind='expense', category='rent', amount=200,
        occurred_on=local_today(salon), concept='x',
    )
    assert owner.get(LIST_URL).data[0]['saldo'] == 300
