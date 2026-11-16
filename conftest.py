from types import SimpleNamespace

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_cache():
    """DRF throttling counts requests in the cache, keyed by scope and IP. The
    test cache is a process-global LocMemCache, so without this every login
    across the whole suite would share one counter and trip the rate limit.
    Clearing per test keeps each one isolated and the throttling real."""
    cache.clear()
    yield
    cache.clear()


# The cast most tests share. A module that needs another country, add-on or role
# defines its own fixture of the same name, which wins over these.

@pytest.fixture
def salon(db):
    from apps.tenancy.models import Tenant

    return Tenant.objects.create(name='Salon', slug='salon', country='AR')


@pytest.fixture
def clinic(db):
    """A second business, for every "never crosses tenants" test."""
    from apps.tenancy.models import Tenant

    return Tenant.objects.create(name='Clinic', slug='clinic', country='AR')


@pytest.fixture
def stylist(db, django_user_model, salon):
    """A staff membership that is booked: the professional in the agenda."""
    from apps.accounts.models import Membership

    user = django_user_model.objects.create_user(email='s@example.com', password='pw')
    return Membership.objects.create(user=user, tenant=salon, attends_appointments=True)


@pytest.fixture
def haircut(db, salon):
    from datetime import timedelta

    from apps.scheduling.models import Service

    return Service.objects.create(tenant=salon, name='Haircut', duration=timedelta(minutes=30))


@pytest.fixture
def bucket(monkeypatch):
    """
    The one fake for images: `storage.head` and `storage.delete` answer from a
    dict instead of the bucket, and every delete is recorded. `presign_put`
    stays real -- it is offline signing. Put an object with
    `bucket.objects[key] = (size, content_type)`; read `bucket.deleted` after.
    """
    from apps.commons import storage

    fake = SimpleNamespace(objects={}, deleted=[])

    def head(key):
        if key not in fake.objects:
            return None
        size, content_type = fake.objects[key]
        return {'size': size, 'content_type': content_type}

    def delete(key):
        fake.deleted.append(key)
        fake.objects.pop(key, None)

    monkeypatch.setattr(storage, 'head', head)
    monkeypatch.setattr(storage, 'delete', delete)
    return fake
