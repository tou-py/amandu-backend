from django.urls import reverse

from apps.accounts.models import Notification
from apps.commons.tests.helpers import api

LIST_URL = reverse('accounts:notification-list')


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
    read_at is the only field mark_all_read touches, and it moves no
    `updated_at`. A timestamp-derived validator had to be taught about that
    column by hand; a body-derived ETag cannot miss it, in the same second or
    any other.
    """
    Notification.objects.create(
        recipient=stylist, verb=Notification.Verb.APPOINTMENT_CANCELLED,
    )
    http = api(stylist.user, stylist.tenant)

    first = http.get(LIST_URL, HTTP_ACCEPT_ENCODING='gzip')
    assert first.data['results'][0]['read_at'] is None

    read_url = reverse('accounts:notification-mark-all-read')
    assert http.post(read_url).status_code == 204

    second = http.get(
        LIST_URL, HTTP_ACCEPT_ENCODING='gzip', HTTP_IF_NONE_MATCH=first.headers['ETag'],
    )
    assert second.status_code == 200
    assert second.data['results'][0]['read_at'] is not None
