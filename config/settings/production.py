"""Production: TLS terminated by Traefik.

Credentials and hosts are not re-declared here: base.py already requires every one of
them from the environment, so there is no permissive fallback left to override.
"""

from datetime import date

from .base import *  # noqa: F403

DEBUG = False

# config for Traefik
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 180
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# The day per-turn charging went live (the deploy of monthly plans), YYYY-MM-DD.
# A turn before it is never reported unpaid: nobody was asked to charge it then,
# and Por cobrar would otherwise open on years of "debts" nobody can reconstruct.
# Required here and only here: a wrong guess silently bills or forgives real
# money, so a deployment that forgot it must not boot. Development and test
# carry a default.
BILLING_GO_LIVE = date.fromisoformat(env.str('BILLING_GO_LIVE'))  # noqa: F405
