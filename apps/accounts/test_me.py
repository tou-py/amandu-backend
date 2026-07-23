import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

ME_URL = reverse('accounts:me')


def api(user=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon')


@pytest.fixture
def clinic(db):
    return Tenant.objects.create(name='Clinic', slug='clinic')


def test_me_requires_authentication(db):
    assert api().get(ME_URL).status_code == 401


def test_me_returns_identity_and_every_active_tenant(db, django_user_model, salon, clinic):
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.OWNER)
    Membership.objects.create(user=user, tenant=clinic, role=Membership.Role.STAFF)

    res = api(user).get(ME_URL)

    assert res.status_code == 200
    assert res.data['email'] == 'u@example.com'
    assert {m['tenant_slug']: m['role'] for m in res.data['memberships']} == {
        'salon': 'owner',
        'clinic': 'staff',
    }


def test_me_does_not_require_a_selected_tenant(db, django_user_model, salon, clinic):
    """No X-Tenant-ID header: /me must still answer, because it is how the client
    learns which tenant to select in the first place."""
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    Membership.objects.create(user=user, tenant=clinic)

    assert api(user).get(ME_URL).status_code == 200


def test_me_hides_a_suspended_membership(db, django_user_model, salon, clinic):
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    Membership.objects.create(
        user=user, tenant=clinic, status=Membership.Status.SUSPENDED
    )

    res = api(user).get(ME_URL)

    assert [m['tenant_slug'] for m in res.data['memberships']] == ['salon']


def test_me_hides_a_suspended_tenant(db, django_user_model, salon):
    """A membership to a suspended tenant is not actionable (HasActiveMembership
    would 403 it), so the switcher must not offer it."""
    closed = Tenant.objects.create(
        name='Closed', slug='closed', status=Tenant.Status.SUSPENDED
    )
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon)
    Membership.objects.create(user=user, tenant=closed)

    res = api(user).get(ME_URL)

    assert [m['tenant_slug'] for m in res.data['memberships']] == ['salon']


def test_a_superuser_has_no_memberships(db, django_user_model):
    superuser = django_user_model.objects.create_superuser(
        email='su@example.com', password='pw'
    )

    res = api(superuser).get(ME_URL)

    assert res.status_code == 200
    assert res.data['memberships'] == []
