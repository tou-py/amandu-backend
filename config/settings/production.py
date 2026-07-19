"""Production: TLS terminated by Traefik, credentials required from the environment."""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

# Every base.py setting that carries a development-friendly fallback is re-read here
# without one, so a missing variable fails at boot instead of at the first request.
# Adding a defaulted setting to base means adding it to this block too.
SECRET_KEY = env.str('SECRET_KEY')
ALLOWED_HOSTS = env.list('ALLOWED_HOSTS')
DATABASES['default']['HOST'] = env.str('POSTGRES_HOST')  # noqa: F405
DATABASES['default']['PORT'] = env.int('POSTGRES_PORT')  # noqa: F405
CACHES['default']['LOCATION'] = env.str('REDIS_URL')  # noqa: F405

# config for Traefik
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 180
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
