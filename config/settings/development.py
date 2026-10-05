"""Local development: runserver, MinIO, the Postgres and Redis from docker-compose."""

from datetime import date

from .base import *  # noqa: F403

# Not read from the environment: the settings module already picks the environment,
# and a DEBUG=False here would mean no tracebacks and none of production's security
# headers either, which is a combination nothing is tested against.
DEBUG = True

# Required in production (see production.py); here any day will do, and .env may
# still move it.
BILLING_GO_LIVE = date.fromisoformat(env.str('BILLING_GO_LIVE', default='2026-10-02'))  # noqa: F405
