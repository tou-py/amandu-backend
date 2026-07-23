"""
Test settings: Redis and S3 are replaced, but Postgres is REAL and required.

The suite used to run on SQLite in memory so it worked anywhere. It cannot anymore:
appointments forbid overlap through an ExclusionConstraint over a tstzrange, which
only Postgres has. Testing that rule on a backend that does not implement it would
mean the one constraint protecting against double-booking is never exercised -- and a
double-booked professional is the worst failure this product can ship.

So `docker compose up -d db` is now a prerequisite for running the tests.
"""

import os

# base.py requires all of these from the environment. Redis and S3 are replaced below,
# so placeholders are enough for them. The POSTGRES_* variables are deliberately NOT
# here: they must come from .env, because the connection is real. Note that
# read_env() does not overwrite os.environ, so a placeholder set here would silently
# win over the developer's .env.
for _key, _value in (
    ('SECRET_KEY', 'test'),
    ('ALLOWED_HOSTS', 'test'),
    ('CORS_ALLOWED_ORIGINS', 'http://test'),
    ('REDIS_URL', 'redis://test:6379/0'),
    ('AWS_S3_ENDPOINT_URL', 'http://test'),
    ('AWS_ACCESS_KEY_ID', 'test'),
    ('AWS_SECRET_ACCESS_KEY', 'test'),
    ('AWS_STORAGE_BUCKET_NAME', 'test'),
):
    os.environ.setdefault(_key, _value)

from .base import *  # noqa: E402, F403

DEBUG = False

CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    # Manifest storage would require a collectstatic run before every test.
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# The default PBKDF2 hasher dominates the runtime of any test that creates a user.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
