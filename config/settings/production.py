"""Production: TLS terminated by Traefik.

Credentials and hosts are not re-declared here: base.py already requires every one of
them from the environment, so there is no permissive fallback left to override.
"""

from .base import *  # noqa: F403

DEBUG = False

# config for Traefik
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 180
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
