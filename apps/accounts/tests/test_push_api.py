import hashlib
from datetime import timedelta
from unittest import mock

import pytest
from django.urls import reverse

from apps.accounts.models import Notification, PushSubscription
from apps.commons.tests.helpers import api

ME_URL = reverse('accounts:me')
PUSH_URL = reverse('accounts:push-subscription')

SUBSCRIPTION = {
    'endpoint': 'https://push.example.com/abc',
    'keys': {'p256dh': 'key-material', 'auth': 'auth-secret'},
}


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user(email='u@example.com', password='pw')


@pytest.fixture
def other(db, django_user_model):
    return django_user_model.objects.create_user(email='o@example.com', password='pw')


def test_stores_a_subscription_against_the_caller(user):
    res = api(user).post(PUSH_URL, SUBSCRIPTION, format='json')

    assert res.status_code == 204
    subscription = PushSubscription.objects.get()
    assert subscription.user == user
    assert subscription.p256dh == 'key-material'
    assert subscription.auth == 'auth-secret'


def test_never_echoes_the_endpoint_back(user):
    """It is a secret (MDN): whoever holds it can push to that browser."""
    res = api(user).post(PUSH_URL, SUBSCRIPTION, format='json')

    assert res.status_code == 204
    assert not res.data


def test_rejects_a_subscription_missing_key_material(user):
    res = api(user).post(
        PUSH_URL,
        {'endpoint': 'https://push.example.com/abc', 'keys': {'p256dh': 'only-one'}},
        format='json',
    )

    assert res.status_code == 400
    assert not PushSubscription.objects.exists()


def test_resubscribing_the_same_browser_does_not_duplicate_it(user):
    """The browser hands back the same endpoint, and two rows would push the same
    person the same notification twice."""
    api(user).post(PUSH_URL, SUBSCRIPTION, format='json')
    second = api(user).post(PUSH_URL, SUBSCRIPTION, format='json')

    # Asserted, not assumed: a 400 would also leave exactly one row, and that is
    # precisely the bug this test caught the first time it ran.
    assert second.status_code == 204
    assert PushSubscription.objects.count() == 1


def test_a_shared_device_follows_whoever_logged_in_last(user, other):
    """Same browser, second account: the row moves. Otherwise the machine keeps
    announcing the previous person's day to the new one."""
    api(user).post(PUSH_URL, SUBSCRIPTION, format='json')
    api(other).post(PUSH_URL, SUBSCRIPTION, format='json')

    assert PushSubscription.objects.count() == 1
    assert PushSubscription.objects.get().user == other


def test_there_is_no_unsubscribe_endpoint(user):
    """Turning reminders off is the browser's own unsubscribe(); the row then
    dies on the next send, when the push service answers 404. Documented here so
    nobody adds a DELETE back without knowing it duplicates that path."""
    api(user).post(PUSH_URL, SUBSCRIPTION, format='json')

    res = api(user).delete(PUSH_URL, {'endpoint': SUBSCRIPTION['endpoint']}, format='json')

    assert res.status_code == 405
    assert PushSubscription.objects.count() == 1


def test_me_carries_the_public_key_and_the_lead_time(user, settings):
    settings.VAPID_PUBLIC_KEY = 'the-public-key'

    res = api(user).get(ME_URL)

    assert res.data['vapid_public_key'] == 'the-public-key'
    assert res.data['reminder_lead'] == '00:30:00'


def test_me_reports_no_public_key_when_push_is_unconfigured(user, settings):
    """Which is how the client knows not to offer reminders at all."""
    settings.VAPID_PUBLIC_KEY = ''

    assert api(user).get(ME_URL).data['vapid_public_key'] == ''


def test_the_lead_time_is_editable_from_the_profile(user):
    res = api(user).patch(ME_URL, {'reminder_lead': '02:00:00'}, format='json')

    assert res.status_code == 200
    user.refresh_from_db()
    assert user.reminder_lead == timedelta(hours=2)


def test_the_public_key_is_not_writable(user, settings):
    settings.VAPID_PUBLIC_KEY = 'the-public-key'

    res = api(user).patch(ME_URL, {'vapid_public_key': 'mine-now'}, format='json')

    assert res.status_code == 200
    assert res.data['vapid_public_key'] == 'the-public-key'


# -- Where my notices go -------------------------------------------------------

def test_lists_my_devices_without_their_endpoints(user, other):
    api(user).post(PUSH_URL, {**SUBSCRIPTION, 'device': 'Chrome en Android'}, format='json')
    api(other).post(PUSH_URL, {**SUBSCRIPTION, 'endpoint': 'https://web.push.apple.com/x'}, format='json')

    res = api(user).get(PUSH_URL)

    assert res.status_code == 200
    assert [(d['device'], d['service']) for d in res.data] == [('Chrome en Android', 'other')]
    assert 'endpoint' not in res.data[0]
    assert res.data[0]['fingerprint'] == hashlib.sha256(SUBSCRIPTION['endpoint'].encode()).hexdigest()


def test_removes_one_of_my_devices_but_never_someone_elses(user, other):
    api(other).post(PUSH_URL, SUBSCRIPTION, format='json')
    theirs = PushSubscription.objects.get()

    assert api(user).delete(reverse('accounts:push-device', args=[theirs.pk])).status_code == 404
    assert api(other).delete(reverse('accounts:push-device', args=[theirs.pk])).status_code == 204
    assert not PushSubscription.objects.exists()


def test_a_test_notice_rings_every_device_and_files_nothing(user):
    api(user).post(PUSH_URL, SUBSCRIPTION, format='json')
    api(user).post(PUSH_URL, {**SUBSCRIPTION, 'endpoint': 'https://push.example.com/def'}, format='json')

    with mock.patch('apps.scheduling.management.commands.send_reminders.webpush') as push:
        res = api(user).post(reverse('accounts:push-test'))

    assert res.data == {'sent': 2, 'devices': 2}
    assert push.call_count == 2
    assert not Notification.objects.exists()
