import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(email='u@example.com', password='pw')


@pytest.mark.django_db
def test_login_returns_token_and_active_memberships(user):
    salon = Tenant.objects.create(name='Salon', slug='salon')
    clinic = Tenant.objects.create(name='Clinic', slug='clinic')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    # Suspended in clinic: must NOT be offered.
    Membership.objects.create(
        user=user,
        tenant=clinic,
        role=Membership.Role.STAFF,
        status=Membership.Status.SUSPENDED,
    )

    res = APIClient().post(
        reverse('accounts:login'),
        {'email': 'u@example.com', 'password': 'pw'},
        format='json',
    )

    assert res.status_code == 200
    assert 'access' in res.data and 'refresh' in res.data
    tenants = {m['tenant_slug']: m['role'] for m in res.data['memberships']}
    assert tenants == {'salon': Membership.Role.OWNER}


@pytest.mark.django_db
def test_login_wrong_password_is_rejected(user):
    res = APIClient().post(
        reverse('accounts:login'),
        {'email': 'u@example.com', 'password': 'wrong'},
        format='json',
    )

    assert res.status_code == 401


@pytest.mark.django_db
def test_superuser_logs_in_with_no_memberships(django_user_model):
    django_user_model.objects.create_superuser(email='root@example.com', password='pw')

    res = APIClient().post(
        reverse('accounts:login'),
        {'email': 'root@example.com', 'password': 'pw'},
        format='json',
    )

    assert res.status_code == 200
    assert res.data['memberships'] == []
