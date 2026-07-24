import pytest
from django.urls import reverse
from rest_framework.test import APIClient


@pytest.mark.django_db
def test_login_is_rate_limited_after_five_attempts():
    """Six wrong-password logins in a row: the sixth is throttled, not just
    rejected. Throttling runs before the view, so a failed attempt still counts
    -- which is the whole point against brute force."""
    client = APIClient()
    url = reverse('accounts:login')
    payload = {'email': 'nobody@example.com', 'password': 'wrong'}

    for _ in range(5):
        assert client.post(url, payload, format='json').status_code == 401

    assert client.post(url, payload, format='json').status_code == 429
