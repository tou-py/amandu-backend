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
