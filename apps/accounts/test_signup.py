from datetime import timedelta
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import CustomUser, Membership
from apps.tenancy.models import Tenant

SIGNUP_URL = reverse('accounts:signup')
LOGIN_URL = reverse('accounts:login')
STRONG_PASSWORD = 'sup3r-secret-pw'

pytestmark = pytest.mark.django_db


def signup(**overrides):
    payload = {
        'business_name': 'Peluquería Iguazú',
        'first_name': 'Ana',
        'email': 'ana@example.com',
        'password': STRONG_PASSWORD,
        'timezone': 'America/Asuncion',
        'country': 'PY',
        **overrides,
    }
    return APIClient().post(SIGNUP_URL, payload, format='json')


def test_signup_creates_a_business_with_its_owner_and_mails_the_link():
    assert signup().status_code == 202

    user = CustomUser.objects.get()
    tenant = Tenant.objects.get()
    assert (user.email, user.first_name) == ('ana@example.com', 'Ana')
    assert user.email_verified_at is None
    assert user.terms_version == 1
    assert user.terms_accepted_at is not None
    assert (tenant.name, tenant.slug) == ('Peluquería Iguazú', 'peluqueria-iguazu')
    assert (tenant.timezone, tenant.country) == ('America/Asuncion', 'PY')
    membership = Membership.objects.get()
    assert (membership.user, membership.tenant) == (user, tenant)
    assert membership.role == Membership.Role.OWNER
    assert membership.attends_appointments
    assert [m.to for m in mail.outbox] == [['ana@example.com']]
    assert '/verificar?token=' in mail.outbox[0].body


def test_signing_up_is_not_yet_a_way_in():
    signup()

    login = APIClient().post(LOGIN_URL, {'email': 'ana@example.com', 'password': STRONG_PASSWORD}, format='json')

    assert login.data['code'] == 'email_not_verified'


def test_the_slug_steps_aside_for_one_taken():
    Tenant.objects.create(name='Iguazú', slug='iguazu')
    Tenant.objects.create(name='Iguazú 2', slug='iguazu-2')

    signup(business_name='Iguazú')

    assert Tenant.objects.filter(slug='iguazu-3').exists()


def test_a_name_with_nothing_to_slug_still_gets_one():
    signup(business_name='日本')

    assert Tenant.objects.filter(name='日本').exclude(slug='').exists()


def test_a_bad_browser_guess_falls_back_instead_of_refusing():
    assert signup(timezone='Mars/Olympus', country='paraguay').status_code == 202

    tenant = Tenant.objects.get()
    assert (tenant.timezone, tenant.country) == ('UTC', '')


def test_a_weak_password_is_refused_on_its_field():
    res = signup(password='123')

    assert res.status_code == 400
    assert 'password' in res.data
    assert not CustomUser.objects.exists()


def test_an_email_already_registered_gets_the_same_answer_and_creates_nothing():
    CustomUser.objects.create_user(email='ana@example.com', password='original-pw-123')

    res = signup(email='ANA@example.com')

    assert res.status_code == 202
    assert CustomUser.objects.count() == 1
    assert not Tenant.objects.exists()
    assert len(mail.outbox) == 1
    assert 'Ya tenés una cuenta' in mail.outbox[0].subject
    assert '/recuperar' in mail.outbox[0].body


def test_signing_up_again_before_verifying_sends_the_link_again():
    signup()

    assert signup(business_name='Otro nombre').status_code == 202

    assert Tenant.objects.get().name == 'Peluquería Iguazú'
    assert [('/verificar?token=' in m.body) for m in mail.outbox] == [True, True]


def test_signup_is_throttled():
    for n in range(5):
        assert signup(email=f'a{n}@example.com').status_code == 202

    assert signup(email='a5@example.com').status_code == 429


def age(user, days):
    CustomUser.objects.filter(pk=user.pk).update(created_at=timezone.now() - timedelta(days=days))


def test_the_sweep_drops_a_signup_unconfirmed_for_a_week_with_its_business(settings):
    settings.VAPID_PRIVATE_KEY = 'k'  # else the command raises, as on CI
    settings.VAPID_SUBJECT = 'mailto:x@example.com'
    signup()
    age(CustomUser.objects.get(), days=8)

    call_command('send_reminders')

    assert not CustomUser.objects.exists()
    assert not Tenant.objects.exists()


def test_the_sweep_leaves_a_recent_signup_and_anyone_confirmed(settings):
    settings.VAPID_PRIVATE_KEY = 'k'  # else the command raises, as on CI
    settings.VAPID_SUBJECT = 'mailto:x@example.com'
    signup()
    age(CustomUser.objects.get(), days=6)
    confirmed = CustomUser.objects.create_user(email='old@example.com', password=STRONG_PASSWORD)
    business = Tenant.objects.create(name='Old', slug='old')
    Membership.objects.create(user=confirmed, tenant=business, role=Membership.Role.OWNER)
    age(confirmed, days=400)

    call_command('send_reminders')

    assert CustomUser.objects.count() == 2
    assert Tenant.objects.count() == 2


def funnel():
    out = StringIO()
    call_command('signup_funnel', stdout=out)
    return dict(line.rsplit(None, 1) for line in out.getvalue().splitlines()[1:])


def test_the_funnel_counts_each_business_at_its_stage():
    signup(email='a@example.com', business_name='Unverified')
    signup(email='b@example.com', business_name='Verified')
    signup(email='c@example.com', business_name='Paying')
    for email in ('b@example.com', 'c@example.com'):
        CustomUser.objects.get(email=email).confirm_email()
    paying = Tenant.objects.get(name='Paying')
    paying.paid_until = paying.trial_until + timedelta(days=30)
    paying.save()
    Tenant.objects.create(name='Set up by hand', slug='by-hand')

    assert funnel() == {'Unverified': '1', 'Verified': '2', 'First turn': '0', 'Paying': '1'}
