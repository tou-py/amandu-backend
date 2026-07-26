import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.tenancy.models import Tenant

ME_URL = reverse('accounts:me')
PASSWORD_URL = reverse('accounts:change-password')
STRONG_PASSWORD = 'sup3r-secret-pw'


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


def test_editing_the_name_relabels_the_person_in_every_agenda(
    db, django_user_model, salon, clinic
):
    """
    `Membership.display_name` reads `get_full_name()`, so the profile name is the
    label every tenant's agenda shows. Editing it is deliberately not per tenant.
    """
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')
    salon_membership = Membership.objects.create(user=user, tenant=salon)
    clinic_membership = Membership.objects.create(user=user, tenant=clinic)

    res = api(user).patch(
        ME_URL, {'first_name': 'Ada', 'last_name': 'Lovelace'}, format='json'
    )

    assert res.status_code == 200
    assert res.data['first_name'] == 'Ada'
    assert salon_membership.display_name() == 'Ada Lovelace'
    assert clinic_membership.display_name() == 'Ada Lovelace'


def test_the_profile_cannot_change_its_own_email(db, django_user_model):
    """email is the USERNAME_FIELD and what invitations were addressed to, so it
    is read-only: DRF drops it rather than failing, and the address must stand."""
    user = django_user_model.objects.create_user(email='u@example.com', password='pw')

    res = api(user).patch(ME_URL, {'email': 'someone.else@example.com'}, format='json')

    assert res.status_code == 200
    user.refresh_from_db()
    assert user.email == 'u@example.com'


def test_editing_a_profile_requires_authentication(db):
    assert api().patch(ME_URL, {'first_name': 'X'}, format='json').status_code == 401


def test_changing_the_password_replaces_the_one_that_logs_in(db, django_user_model):
    user = django_user_model.objects.create_user(email='u@example.com', password='old-pw')

    res = api(user).post(
        PASSWORD_URL,
        {'current_password': 'old-pw', 'new_password': STRONG_PASSWORD},
        format='json',
    )

    assert res.status_code == 204
    user.refresh_from_db()
    assert user.check_password(STRONG_PASSWORD)
    assert not user.check_password('old-pw')


def test_the_current_password_is_required_to_change_it(db, django_user_model):
    """A valid token proves the session was opened by the owner once; it must not
    be enough to take the account over."""
    user = django_user_model.objects.create_user(email='u@example.com', password='old-pw')

    res = api(user).post(
        PASSWORD_URL,
        {'current_password': 'not-the-password', 'new_password': STRONG_PASSWORD},
        format='json',
    )

    assert res.status_code == 400
    assert 'current_password' in res.data
    user.refresh_from_db()
    assert user.check_password('old-pw')


def test_a_weak_new_password_is_refused(db, django_user_model):
    user = django_user_model.objects.create_user(email='u@example.com', password='old-pw')

    res = api(user).post(
        PASSWORD_URL,
        {'current_password': 'old-pw', 'new_password': '123'},
        format='json',
    )

    assert res.status_code == 400
    assert 'new_password' in res.data
    user.refresh_from_db()
    assert user.check_password('old-pw')


def test_changing_a_password_requires_authentication(db):
    res = api().post(
        PASSWORD_URL,
        {'current_password': 'x', 'new_password': STRONG_PASSWORD},
        format='json',
    )

    assert res.status_code == 401


def test_a_superuser_has_no_memberships(db, django_user_model):
    superuser = django_user_model.objects.create_superuser(
        email='su@example.com', password='pw'
    )

    res = api(superuser).get(ME_URL)

    assert res.status_code == 200
    assert res.data['memberships'] == []
