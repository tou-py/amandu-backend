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
