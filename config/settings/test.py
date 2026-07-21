"""Test settings: no external services, so the suite runs anywhere."""

import os

# base.py requires all of these from the environment. Tests never reach Postgres,
# Redis or S3 -- all three are replaced below -- so placeholders let the suite run on
# a machine with no .env. Adding a required setting to base breaks the tests here
# immediately and by name, which is why this list is safe to maintain by hand.
for _key, _value in (
    ('SECRET_KEY', 'test'),
    ('ALLOWED_HOSTS', 'test'),
    ('CORS_ALLOWED_ORIGINS', 'http://test'),
    ('POSTGRES_DB', 'test'),
    ('POSTGRES_USER', 'test'),
    ('POSTGRES_PASSWORD', 'test'),
    ('POSTGRES_HOST', 'test'),
    ('REDIS_URL', 'redis://test:6379/0'),
    ('AWS_S3_ENDPOINT_URL', 'http://test'),
    ('AWS_ACCESS_KEY_ID', 'test'),
    ('AWS_SECRET_ACCESS_KEY', 'test'),
    ('AWS_STORAGE_BUCKET_NAME', 'test'),
):
    os.environ.setdefault(_key, _value)

from .base import *  # noqa: E402, F403

DEBUG = False

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    # Manifest storage would require a collectstatic run before every test.
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}

# The default PBKDF2 hasher dominates the runtime of any test that creates a user.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
