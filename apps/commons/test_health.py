import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_healthz_is_ok_without_auth_when_db_is_reachable(client):
    """Anonymous client, no token, no X-Tenant-ID -- an orchestrator has none of
    those -- still gets 200 as long as the database answers."""
    res = client.get(reverse('healthz'))

    assert res.status_code == 200
    assert res.json() == {'status': 'ok'}
