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


def test_another_tenants_service_is_not_reachable_by_id(receptionist, salon, clinic):
    foreign = Service.objects.create(
        tenant=clinic, name='Massage', duration=timedelta(minutes=60)
    )

    res = api(receptionist, salon).get(
        reverse('scheduling:service-detail', args=[foreign.pk])
    )

    assert res.status_code == 404
