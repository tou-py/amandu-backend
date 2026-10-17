from datetime import timedelta

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Category, Service
from apps.tenancy.models import Tenant

CATEGORY_LIST = reverse('scheduling:category-list')
SERVICE_LIST = reverse('scheduling:service-list')


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def receptionist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='r@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    return user


def test_category_create_assigns_the_tenant_from_the_request(receptionist, salon):
    res = api(receptionist, salon).post(CATEGORY_LIST, {'name': 'Hair'}, format='json')

    assert res.status_code == 201
    assert Category.objects.get(name='Hair').tenant == salon


def test_same_category_name_is_allowed_in_another_tenant(receptionist, salon, clinic):
    Membership.objects.create(user=receptionist, tenant=clinic)
    api(receptionist, salon).post(CATEGORY_LIST, {'name': 'Hair'}, format='json')

    res = api(receptionist, clinic).post(CATEGORY_LIST, {'name': 'Hair'}, format='json')

    assert res.status_code == 201
    assert Category.objects.count() == 2


def test_service_create_with_duration_and_category(receptionist, salon):
    category = Category.objects.create(tenant=salon, name='Hair')

    res = api(receptionist, salon).post(
        SERVICE_LIST,
        {'name': 'Haircut', 'duration': '00:30:00', 'category': category.pk},
        format='json',
    )

    assert res.status_code == 201
    service = Service.objects.get(name='Haircut')
    assert service.tenant == salon
    assert service.duration == timedelta(minutes=30)
    assert service.category == category


def test_service_may_have_no_category(receptionist, salon):
    res = api(receptionist, salon).post(
        SERVICE_LIST, {'name': 'Haircut', 'duration': '00:30:00'}, format='json'
    )

    assert res.status_code == 201
    assert Service.objects.get(name='Haircut').category is None


def test_a_foreign_tenants_category_cannot_be_attached(receptionist, salon, clinic):
    """
    a category id from another tenant is a 400, not a silent cross-tenant link.
    """
    foreign = Category.objects.create(tenant=clinic, name='Nails')

    res = api(receptionist, salon).post(
        SERVICE_LIST,
        {'name': 'Manicure', 'duration': '00:45:00', 'category': foreign.pk},
        format='json',
    )

    assert res.status_code == 400
    assert 'category' in res.data


def test_deleting_a_category_leaves_the_service_without_a_label(receptionist, salon):
    """on_delete=SET_NULL: the service survives, it just loses its category."""
    category = Category.objects.create(tenant=salon, name='Hair')
    service = Service.objects.create(
        tenant=salon, name='Haircut', duration=timedelta(minutes=30), category=category
    )

    category.delete()

    service.refresh_from_db()
    assert service.category is None


def test_service_list_only_returns_the_active_tenants_services(receptionist, salon, clinic):
    Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))
    Service.objects.create(tenant=clinic, name='Massage', duration=timedelta(minutes=60))

    res = api(receptionist, salon).get(SERVICE_LIST)

    assert res.status_code == 200
    assert [s['name'] for s in res.data['results']] == ['Haircut']


def test_a_duplicate_name_is_a_400_not_a_crash(receptionist, salon):
    """
    (tenant, name) is unique, but `tenant` is not a serializer field, so DRF
    builds no UniqueTogetherValidator for it and the duplicate used to reach the
    database as an IntegrityError -- a 500 for an ordinary typo.
    """
    Category.objects.create(tenant=salon, name='Hair')
    Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))

    category = api(receptionist, salon).post(CATEGORY_LIST, {'name': 'Hair'}, format='json')
    service = api(receptionist, salon).post(
        SERVICE_LIST, {'name': 'Haircut', 'duration': '00:45:00'}, format='json'
    )

    assert category.status_code == 400
    assert 'name' in category.data
    assert service.status_code == 400
    assert 'name' in service.data


def test_a_name_that_differs_only_in_case_is_refused(receptionist, salon):
    """Stricter than the constraint on purpose: nobody can tell the two apart."""
    Category.objects.create(tenant=salon, name='Hair')

    res = api(receptionist, salon).post(CATEGORY_LIST, {'name': 'hair'}, format='json')

    assert res.status_code == 400


def test_saving_a_row_under_its_own_name_is_not_a_duplicate(receptionist, salon):
    """The check must exclude the instance being edited, or no edit could save."""
    service = Service.objects.create(
        tenant=salon, name='Haircut', duration=timedelta(minutes=30),
        created_by=receptionist.memberships.get(),
    )

    res = api(receptionist, salon).patch(
        reverse('scheduling:service-detail', args=[service.pk]),
        {'name': 'Haircut', 'duration': '00:45:00'},
        format='json',
    )

    assert res.status_code == 200
    service.refresh_from_db()
    assert service.duration == timedelta(minutes=45)


def test_another_tenants_name_is_not_a_duplicate(receptionist, salon, clinic):
    """The uniqueness is per tenant; the check must be scoped the same way."""
    Service.objects.create(tenant=clinic, name='Haircut', duration=timedelta(minutes=30))

    res = api(receptionist, salon).post(
        SERVICE_LIST, {'name': 'Haircut', 'duration': '00:30:00'}, format='json'
    )

    assert res.status_code == 201


def test_another_tenants_service_is_not_reachable_by_id(receptionist, salon, clinic):
    foreign = Service.objects.create(
        tenant=clinic, name='Massage', duration=timedelta(minutes=60)
    )

    res = api(receptionist, salon).get(
        reverse('scheduling:service-detail', args=[foreign.pk])
    )

    assert res.status_code == 404


def test_a_service_round_trips_with_and_without_a_price(receptionist, salon):
    """
    The two shapes ARE the two billing modes: a priced service is charged per
    session, a null price says this one is not. There is no flag saying which,
    so both have to survive the round trip intact.
    """
    http = api(receptionist, salon)

    priced = http.post(
        SERVICE_LIST, {'name': 'Haircut', 'duration': '00:30:00', 'price': 120000},
        format='json',
    )
    unpriced = http.post(
        SERVICE_LIST, {'name': 'Pilates', 'duration': '01:00:00'}, format='json',
    )

    assert priced.status_code == 201
    assert priced.data['price'] == 120000
    assert Service.objects.get(name='Haircut').price == 120000

    assert unpriced.status_code == 201
    assert unpriced.data['price'] is None
    assert Service.objects.get(name='Pilates').price is None


def _member(django_user_model, tenant, email, role):
    user = django_user_model.objects.create_user(email=email, password='pw')
    return Membership.objects.create(user=user, tenant=tenant, role=role)


def test_staff_keep_the_services_they_create_and_only_those(django_user_model, salon):
    ana = _member(django_user_model, salon, 'ana@example.com', Membership.Role.STAFF)
    bea = _member(django_user_model, salon, 'bea@example.com', Membership.Role.STAFF)
    res = api(ana.user, salon).post(SERVICE_LIST, {'name': 'Cut', 'duration': '00:30:00'}, format='json')
    assert res.status_code == 201
    assert res.data['created_by'] == ana.pk
    own = reverse('scheduling:service-detail', args=[res.data['id']])
    legacy = Service.objects.create(tenant=salon, name='Old', duration=timedelta(minutes=30))
    legacy_url = reverse('scheduling:service-detail', args=[legacy.pk])

    assert api(ana.user, salon).patch(own, {'price': 100}, format='json').status_code == 200
    assert api(bea.user, salon).patch(own, {'price': 1}, format='json').status_code == 403
    assert api(bea.user, salon).delete(own).status_code == 403
    assert api(ana.user, salon).patch(legacy_url, {'price': 1}, format='json').status_code == 403
    assert api(bea.user, salon).get(own).status_code == 200
    assert api(ana.user, salon).delete(own).status_code == 204


@pytest.mark.parametrize('role', [Membership.Role.COORDINATOR, Membership.Role.ADMIN, Membership.Role.OWNER])
def test_the_team_roles_manage_every_service(django_user_model, salon, role):
    lead = _member(django_user_model, salon, 'lead@example.com', role)
    legacy = Service.objects.create(tenant=salon, name='Old', duration=timedelta(minutes=30))
    url = reverse('scheduling:service-detail', args=[legacy.pk])

    assert api(lead.user, salon).patch(url, {'price': 1}, format='json').status_code == 200
    assert api(lead.user, salon).delete(url).status_code == 204
