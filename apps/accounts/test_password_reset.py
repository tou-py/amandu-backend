import re

import pytest
from django.core import mail
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import CustomUser, Membership
from apps.tenancy.models import Tenant

RESET_URL = reverse('accounts:password-reset')
CONFIRM_URL = reverse('accounts:password-reset-confirm')
LOGIN_URL = reverse('accounts:login')
REFRESH_URL = reverse('accounts:token-refresh')
OLD = 'original-pw-123'
NEW = 'sup3r-secret-pw'

pytestmark = pytest.mark.django_db


@pytest.fixture
def ana():
    return CustomUser.objects.create_user(email='ana@example.com', password=OLD)


def ask(email='ana@example.com'):
    return APIClient().post(RESET_URL, {'email': email}, format='json')


def link():
    """uid and token, read out of the mail the way the person gets them."""
    return dict(re.findall(r'(uid|token)=([^&\s]+)', mail.outbox[-1].body))


def confirm(password=NEW, **parts):
    return APIClient().post(CONFIRM_URL, {**link(), 'password': password, **parts}, format='json')


def login(password):
    return APIClient().post(LOGIN_URL, {'email': 'ana@example.com', 'password': password}, format='json')


def test_the_link_sets_the_new_password_and_signs_in(ana):
    assert ask().status_code == 204
    assert '/restablecer?uid=' in mail.outbox[0].body

    res = confirm()

    assert res.status_code == 200
    assert res.data['access'] and res.data['refresh']
    assert login(NEW).status_code == 200
    assert login(OLD).status_code == 401


def test_the_link_works_once(ana):
    ask()
    confirm()

    res = confirm(password='another-pw-456')

    assert res.status_code == 400
    assert res.data['code'] == 'token_invalid'
    assert login(NEW).status_code == 200


def test_a_weak_password_is_refused_and_leaves_the_link_usable(ana):
    ask()

    res = confirm(password='123')

    assert res.status_code == 400
    assert 'password' in res.data
    assert confirm().status_code == 200


def test_resetting_signs_out_every_open_session(ana):
    refresh = login(OLD).data['refresh']
    ask()
    confirm()

    assert APIClient().post(REFRESH_URL, {'refresh': refresh}, format='json').status_code == 401


def test_asking_says_nothing_about_who_exists():
    assert ask('nobody@example.com').status_code == 204

    assert mail.outbox == []


def test_resetting_an_unverified_signup_verifies_it_and_starts_its_trial():
    user = CustomUser.objects.create_user(email='ana@example.com', password=OLD, email_verified_at=None)
    tenant = Tenant.objects.create(name='Iguazú', slug='iguazu')
    Membership.objects.create(user=user, tenant=tenant, role=Membership.Role.OWNER)
    ask()

    assert confirm().status_code == 200

    user.refresh_from_db()
    tenant.refresh_from_db()
    assert user.email_verified_at is not None
    assert tenant.trial_until is not None
