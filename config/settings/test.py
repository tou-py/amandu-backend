"""Test settings: no external services, so the suite runs anywhere."""

import os

# base.py demands real credentials at import time. Tests never reach Postgres or S3
# (both are replaced below), so placeholders are enough when there is no .env.
for _key in (
    'SECRET_KEY', 'POSTGRES_DB', 'POSTGRES_USER', 'POSTGRES_PASSWORD',
    'AWS_S3_ENDPOINT_URL', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'AWS_STORAGE_BUCKET_NAME',
):
    os.environ.setdefault(_key, 'test')

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
