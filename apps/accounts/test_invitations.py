import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import CustomUser, Invitation, Membership
from apps.tenancy.models import Tenant

INVITE_LIST = reverse('accounts:invitation-list')
ACCEPT_URL = reverse('accounts:invitation-accept')
LOGIN_URL = reverse('accounts:login')
STRONG_PASSWORD = 'sup3r-secret-pw'


def detail_url(invitation):
    return reverse('accounts:invitation-detail', args=[invitation.pk])


def api(user=None, tenant=None):
    http = APIClient()
    if user is not None:
        http.force_authenticate(user=user)
    if tenant is not None:
        http.credentials(HTTP_X_TENANT_ID=str(tenant.pk))
    return http


@pytest.fixture
def salon(db):
    return Tenant.objects.create(name='Salon', slug='salon')


@pytest.fixture
def admin_user(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='admin@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.ADMIN)
    return user


@pytest.fixture
def staff_user(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='staff@example.com', password='pw')
    Membership.objects.create(user=user, tenant=salon, role=Membership.Role.STAFF)
    return user


def test_admin_invites_and_gets_a_token_back(admin_user, salon):
    res = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'new@example.com', 'role': 'staff'}, format='json'
    )

    assert res.status_code == 201
    assert res.data['token']
    invitation = Invitation.objects.get(email='new@example.com', tenant=salon)
    assert invitation.status == Invitation.Status.PENDING
    assert invitation.invited_by.user == admin_user


def test_a_staff_member_cannot_invite(staff_user, salon):
    res = api(staff_user, salon).post(
        INVITE_LIST, {'email': 'x@example.com', 'role': 'staff'}, format='json'
    )

    assert res.status_code == 403


def test_listing_invitations_requires_auth(db, salon):
    res = api(tenant=salon).get(INVITE_LIST)

    assert res.status_code == 401


def test_cannot_invite_someone_who_already_has_a_membership(admin_user, salon, staff_user):
    res = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'staff@example.com', 'role': 'staff'}, format='json'
    )

    assert res.status_code == 400


def test_reinviting_refreshes_the_same_pending_row(admin_user, salon):
    http = api(admin_user, salon)
    first = http.post(INVITE_LIST, {'email': 'x@example.com', 'role': 'staff'}, format='json')
    second = http.post(INVITE_LIST, {'email': 'x@example.com', 'role': 'admin'}, format='json')

    assert second.status_code == 201
    assert Invitation.objects.filter(email='x@example.com', tenant=salon).count() == 1
    assert first.data['token'] != second.data['token']
    assert Invitation.objects.get(email='x@example.com', tenant=salon).role == 'admin'


def test_accept_creates_the_user_and_an_active_membership(admin_user, salon):
    token = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'new@example.com', 'role': 'staff'}, format='json'
    ).data['token']

    accept = api().post(
        ACCEPT_URL, {'token': token, 'password': STRONG_PASSWORD}, format='json'
    )

    assert accept.status_code == 204
    user = CustomUser.objects.get(email='new@example.com')
    assert user.has_usable_password()
    membership = Membership.objects.get(user=user, tenant=salon)
    assert membership.status == Membership.Status.ACTIVE
    assert membership.role == 'staff'
    login = api().post(
        LOGIN_URL, {'email': 'new@example.com', 'password': STRONG_PASSWORD}, format='json'
    )
    assert login.status_code == 200


def test_accepting_as_an_existing_user_does_not_reset_their_password(admin_user, salon, django_user_model):
    other = Tenant.objects.create(name='Clinic', slug='clinic')
    existing = django_user_model.objects.create_user(
        email='bob@example.com', password='original-pw-123'
    )
    Membership.objects.create(user=existing, tenant=other, role=Membership.Role.STAFF)
    token = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'bob@example.com', 'role': 'staff'}, format='json'
    ).data['token']

    accept = api().post(
        ACCEPT_URL, {'token': token, 'password': 'attacker-chosen-pw'}, format='json'
    )

    assert accept.status_code == 204
    existing.refresh_from_db()
    assert existing.check_password('original-pw-123')
    assert Membership.objects.filter(user=existing, tenant=salon).exists()


def test_accept_requires_a_password_for_a_new_user(admin_user, salon):
    token = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'new@example.com', 'role': 'staff'}, format='json'
    ).data['token']

    res = api().post(ACCEPT_URL, {'token': token}, format='json')

    assert res.status_code == 400
    assert 'password' in res.data


def test_accept_rejects_a_weak_password(admin_user, salon):
    token = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'new@example.com', 'role': 'staff'}, format='json'
    ).data['token']

    res = api().post(ACCEPT_URL, {'token': token, 'password': '123'}, format='json')

    assert res.status_code == 400


def test_accept_with_an_unknown_token_is_rejected(db):
    res = api().post(ACCEPT_URL, {'token': 'nope', 'password': STRONG_PASSWORD}, format='json')

    assert res.status_code == 400


def test_a_token_cannot_be_used_twice(admin_user, salon):
    token = api(admin_user, salon).post(
        INVITE_LIST, {'email': 'new@example.com', 'role': 'staff'}, format='json'
    ).data['token']

    first = api().post(ACCEPT_URL, {'token': token, 'password': STRONG_PASSWORD}, format='json')
    second = api().post(ACCEPT_URL, {'token': token, 'password': STRONG_PASSWORD}, format='json')

    assert first.status_code == 204
    assert second.status_code == 400


def test_revoking_removes_it_from_the_list_and_blocks_accept(admin_user, salon):
    http = api(admin_user, salon)
    created = http.post(INVITE_LIST, {'email': 'x@example.com', 'role': 'staff'}, format='json')
    invitation = Invitation.objects.get(pk=created.data['id'])

    delete = http.delete(detail_url(invitation))

    assert delete.status_code == 204
    invitation.refresh_from_db()
    assert invitation.status == Invitation.Status.REVOKED
    assert http.get(INVITE_LIST).data['count'] == 0
    accept = api().post(
        ACCEPT_URL, {'token': created.data['token'], 'password': STRONG_PASSWORD}, format='json'
    )
    assert accept.status_code == 400


def test_list_shows_only_pending_invitations(admin_user, salon):
    http = api(admin_user, salon)
    http.post(INVITE_LIST, {'email': 'a@example.com', 'role': 'staff'}, format='json')
    accepted_token = http.post(
        INVITE_LIST, {'email': 'b@example.com', 'role': 'staff'}, format='json'
    ).data['token']
    api().post(ACCEPT_URL, {'token': accepted_token, 'password': STRONG_PASSWORD}, format='json')

    res = http.get(INVITE_LIST)

    assert res.data['count'] == 1
    assert res.data['results'][0]['email'] == 'a@example.com'
