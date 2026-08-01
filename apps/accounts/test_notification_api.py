import pytest
from django.urls import reverse
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
    """Same ETag/If-None-Match contract as AppointmentViewSet."""
    Notification.objects.create(
        recipient=stylist, verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    http = api(stylist.user, stylist.tenant)

    first = http.get(LIST_URL, HTTP_ACCEPT_ENCODING='gzip')
    assert first.status_code == 200
    assert first.headers.get('ETag')
    assert first.headers['Cache-Control'] == 'private, no-cache'

    second = http.get(
        LIST_URL, HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH=first.headers['ETag'],
    )
    assert second.status_code == 304


def test_marking_read_is_visible_on_the_very_next_poll(stylist):
    """
    read_at is the only field mark_read touches, and it moves no `updated_at`.
    A timestamp-derived validator had to be taught about that column by hand;
    a body-derived ETag cannot miss it, in the same second or any other.
    """
    notification = Notification.objects.create(
        recipient=stylist, verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    http = api(stylist.user, stylist.tenant)

    first = http.get(LIST_URL, HTTP_ACCEPT_ENCODING='gzip')
    assert first.data['results'][0]['read_at'] is None

    read_url = reverse('accounts:notification-mark-read', args=[notification.pk])
    assert http.post(read_url).status_code == 200

    second = http.get(
        LIST_URL, HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH=first.headers['ETag'],
    )
    assert second.status_code == 200
    assert second.data['results'][0]['read_at'] is not None
