from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone
from django.utils.http import http_date
from rest_framework.test import APIClient

from apps.accounts.models import Membership, Notification
from apps.tenancy.models import Tenant

LIST_URL = reverse('accounts:notification-list')


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
def stylist(db, django_user_model, salon):
    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


def test_the_list_answers_304_when_nothing_changed_since(stylist):
    """
    Same Last-Modified/If-Modified-Since contract as AppointmentViewSet, but
    Notification has no `updated_at` -- get_last_modified must fall back to the
    newer of `created_at`/`read_at` instead of the mixin's default field.
    """
    Notification.objects.create(
        recipient=stylist, verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    http = api(stylist.user, stylist.tenant)

    first = http.get(LIST_URL)
    assert first.status_code == 200
    assert first.headers.get('Last-Modified')

    second = http.get(LIST_URL, HTTP_IF_MODIFIED_SINCE=first.headers['Last-Modified'])
    assert second.status_code == 304


def test_last_modified_reflects_read_at_when_its_newer_than_created_at(stylist):
    """
    read_at is the only field mark_read touches -- if Last-Modified only looked
    at created_at (the mixin's default field), a client would keep getting 304
    after marking a notification read and never see its own read state
    confirmed on the next poll. Set directly (bypassing auto_now_add) so the
    two timestamps are unambiguously ordered instead of racing the clock.
    """
    notification = Notification.objects.create(
        recipient=stylist, verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    read_later = timezone.now() + timedelta(hours=1)
    Notification.objects.filter(pk=notification.pk).update(read_at=read_later)

    res = api(stylist.user, stylist.tenant).get(LIST_URL)

    assert res.headers['Last-Modified'] == http_date(read_later.timestamp())
