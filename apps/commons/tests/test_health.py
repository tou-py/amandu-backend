import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_healthz_is_ok_without_auth_when_dependencies_are_reachable(client):
    """Anonymous client, no token, no X-Tenant-ID -- an orchestrator has none of
    those -- still gets 200 as long as the database and cache answer."""
    res = client.get(reverse('healthz'))

    assert res.status_code == 200
    assert res.json() == {'status': 'ok', 'database': 'ok', 'cache': 'ok'}


@pytest.mark.django_db
def test_healthz_is_503_when_the_cache_is_unreachable(client, monkeypatch):
    """Every DRF request reads the throttle cache, so a cache outage is a full API
    outage. The probe has to fail with it, otherwise the orchestrator keeps a dead
    instance in rotation."""

    class BrokenCache:
        def get(self, *args, **kwargs):
            raise ConnectionError('redis unreachable')

    monkeypatch.setattr('apps.commons.health.cache', BrokenCache())

    res = client.get(reverse('healthz'))

    assert res.status_code == 503
    assert res.json() == {
        'status': 'unavailable',
        'database': 'ok',
        'cache': 'unavailable',
    }
