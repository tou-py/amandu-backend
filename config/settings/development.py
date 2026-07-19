"""Local development: runserver, MinIO, the Postgres and Redis from docker-compose."""

from .base import *  # noqa: F403

# Not read from the environment: the settings module already picks the environment,
# and a DEBUG=False here would mean no tracebacks and none of production's security
# headers either, which is a combination nothing is tested against.
DEBUG = True
