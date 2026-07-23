import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.scheduling.models import Client
from apps.tenancy.models import Tenant

LIST_URL = reverse('scheduling:client-list')


def detail_url(client):
    return reverse('scheduling:client-detail', args=[client.pk])


def api(user=None, tenant=None):
    """Authenticated caller acting for a tenant, which is how every request arrives."""
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


def test_create_assigns_the_tenant_from_the_request(receptionist, salon):
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').tenant == salon


def test_tenant_cannot_be_set_from_the_payload(receptionist, salon, clinic):
    """editable=False keeps `tenant` out of the serializer, so this is silently ignored."""
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'tenant': clinic.pk}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').tenant == salon


def test_local_phone_is_normalised_with_the_tenant_region(receptionist, salon):
    """
    The whole point of storing E.164: typed the way a person dictates it, stored the
    way the uniqueness constraint can compare it.
    """
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '01112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.get(name='Ada').phone == '+541112345678'


def test_same_person_typed_two_ways_is_rejected_as_duplicate(receptionist, salon):
    http = api(receptionist, salon)
    http.post(LIST_URL, {'name': 'Ada', 'phone': '+54 11 1234-5678'}, format='json')

    res = http.post(LIST_URL, {'name': 'Ada Lovelace', 'phone': '01112345678'}, format='json')

    assert res.status_code == 400
    assert 'phone' in res.data


def test_the_same_phone_may_exist_in_another_tenant(receptionist, salon, clinic):
    Membership.objects.create(user=receptionist, tenant=clinic)
    http = api(receptionist, salon)
    http.post(LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json')

    res = api(receptionist, clinic).post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert res.status_code == 201
    assert Client.objects.count() == 2


def test_many_clients_may_have_no_phone(receptionist, salon):
    """The constraint is partial: empty must not collide with empty."""
    http = api(receptionist, salon)

    first = http.post(LIST_URL, {'name': 'Ada'}, format='json')
    second = http.post(LIST_URL, {'name': 'Grace'}, format='json')

    assert (first.status_code, second.status_code) == (201, 201)


def test_invalid_phone_is_rejected(receptionist, salon):
    res = api(receptionist, salon).post(
        LIST_URL, {'name': 'Ada', 'phone': '123'}, format='json'
    )

    assert res.status_code == 400


def test_list_only_returns_the_active_tenants_clients(receptionist, salon, clinic):
    Client.objects.create(tenant=salon, name='Ada')
    Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).get(LIST_URL)

    assert res.status_code == 200
    assert [c['name'] for c in res.data['results']] == ['Ada']


def test_a_client_of_another_tenant_is_not_reachable_by_id(receptionist, salon, clinic):
    """Holding a valid id from another tenant must be worth nothing."""
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).get(detail_url(foreign))

    assert res.status_code == 404


def test_another_tenants_client_cannot_be_updated(receptionist, salon, clinic):
    foreign = Client.objects.create(tenant=clinic, name='Grace')

    res = api(receptionist, salon).patch(
        detail_url(foreign), {'name': 'Hacked'}, format='json'
    )

    assert res.status_code == 404
    foreign.refresh_from_db()
    assert foreign.name == 'Grace'


def test_anonymous_gets_401_not_403(db, salon):
    res = api(tenant=salon).get(LIST_URL)

    assert res.status_code == 401


def test_authenticated_without_a_membership_gets_403(db, django_user_model, salon):
    outsider = django_user_model.objects.create_user(
        email='out@example.com', password='pw'
    )

    res = api(outsider, salon).get(LIST_URL)

    assert res.status_code == 403


def test_client_ids_are_uuid7(receptionist, salon):
    res = api(receptionist, salon).post(LIST_URL, {'name': 'Ada'}, format='json')

    assert Client.objects.get(pk=res.data['id']).id.version == 7


def test_without_a_tenant_country_a_local_number_is_rejected(db, django_user_model):
    """
    Asserts the mechanism, not just the happy outcome: with no region there is
    nothing to resolve a local format against, so the number must arrive
    international. If this ever passes, the region is coming from somewhere global
    and every tenant is being parsed as the same country.
    """
    nowhere = Tenant.objects.create(name='Nowhere', slug='nowhere')
    user = django_user_model.objects.create_user(email='n@example.com', password='pw')
    Membership.objects.create(user=user, tenant=nowhere)
    http = api(user, nowhere)

    local = http.post(LIST_URL, {'name': 'Ada', 'phone': '01112345678'}, format='json')
    international = http.post(
        LIST_URL, {'name': 'Ada', 'phone': '+541112345678'}, format='json'
    )

    assert local.status_code == 400
    assert international.status_code == 201
