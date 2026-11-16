from django.conf import settings
from django.urls import reverse


def test_preflight_allows_the_tenant_header(client):
    """A browser client selects its tenant with X-Tenant-ID, which is NOT one of
    the six headers django-cors-headers allows by default. Without it in
    CORS_ALLOW_HEADERS the preflight still returns 200 -- it just omits the
    header -- and the browser blocks the real request before sending it. Nothing
    server-to-server ever notices, so only a browser catches the regression."""
    res = client.options(
        reverse('accounts:me'),
        HTTP_ORIGIN=settings.CORS_ALLOWED_ORIGINS[0],
        HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET',
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS='authorization,x-tenant-id',
    )

    assert res.status_code == 200
    allowed = res.headers['access-control-allow-headers'].lower()
    assert 'x-tenant-id' in allowed
    assert 'authorization' in allowed
