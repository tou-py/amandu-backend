import re
from datetime import timedelta

import pytest
from django.core import mail
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import signup
from apps.accounts.models import CustomUser
from apps.tenancy.models import Tenant

SIGNUP_URL = reverse('accounts:signup')
VERIFY_URL = reverse('accounts:verify-email')
RESEND_URL = reverse('accounts:verify-email-resend')
LOGIN_URL = reverse('accounts:login')
PASSWORD = 'sup3r-secret-pw'

pytestmark = pytest.mark.django_db


def sign_up():
    """The token, read the way the person reads it: out of the mail."""
    APIClient().post(SIGNUP_URL, {
        'business_name': 'Iguazú', 'first_name': 'Ana', 'email': 'ana@example.com', 'password': PASSWORD,
    }, format='json')
    return re.search(r'token=(\S+)', mail.outbox[-1].body).group(1)


def verify(token):
    return APIClient().post(VERIFY_URL, {'token': token}, format='json')


def test_the_link_signs_in_and_starts_the_trial_with_every_addon():
    res = verify(sign_up())

    assert res.status_code == 200
    assert res.data['access'] and res.data['refresh']
    assert res.data['memberships'][0]['tenant_slug'] == 'iguazu'
    tenant = Tenant.objects.get()
    assert tenant.trial_until == timezone.localdate() + timedelta(days=14)
    assert tenant.paid_until == tenant.trial_until
    assert tenant.addon_cobros
    assert tenant.addon_reservas
    assert not tenant.addon_facturacion


def test_after_the_link_the_password_works():
    verify(sign_up())

    login = APIClient().post(LOGIN_URL, {'email': 'ana@example.com', 'password': PASSWORD}, format='json')

    assert login.status_code == 200


def test_a_replayed_link_is_not_a_login():
    token = sign_up()
    verify(token)

    res = verify(token)

    assert res.status_code == 409
    assert res.data['code'] == 'already_verified'
    assert 'access' not in res.data


def test_an_expired_link_is_refused(monkeypatch):
    token = sign_up()
    monkeypatch.setattr(signup, 'VERIFY_MAX_AGE', -1)

    res = verify(token)

    assert res.status_code == 400
    assert res.data['code'] == 'token_invalid'
    assert CustomUser.objects.get().email_verified_at is None


def test_a_forged_link_is_refused():
    assert verify('nonsense').data['code'] == 'token_invalid'


def test_resending_mails_a_link_that_works():
    sign_up()

    res = APIClient().post(RESEND_URL, {'email': 'ANA@example.com'}, format='json')

    assert res.status_code == 204
    assert len(mail.outbox) == 2
    assert verify(re.search(r'token=(\S+)', mail.outbox[-1].body).group(1)).status_code == 200


def test_resending_says_nothing_about_who_exists():
    CustomUser.objects.create_user(email='known@example.com', password=PASSWORD)

    for email in ('known@example.com', 'nobody@example.com'):
        assert APIClient().post(RESEND_URL, {'email': email}, format='json').status_code == 204

    assert mail.outbox == []
