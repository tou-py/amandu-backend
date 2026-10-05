FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

WORKDIR /usr/src/app

# PYTHONDONTWRITEBYTECODE=1: Prevents Python from writing .pyc files, reducing unnecessary disk usage.
# PYTHONUNBUFFERED=1: Ensures that the Python output (e.g., print statements) is immediately flushed to the terminal/logs, making debugging easier.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# UV_LINK_MODE=copy: the uv cache lives on a separate mount, so hardlinking is impossible.
ENV UV_LINK_MODE=copy

# Only the lockfile inputs, so this layer is reused whenever dependencies are unchanged.
COPY pyproject.toml uv.lock ./

FROM base AS dev

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY . .

# The same entrypoint prod uses, for the same reason: a stack whose database
# volume is new comes up against an empty schema and 500s on every request until
# someone remembers to migrate by hand. It lives at / rather than in the workdir
# because the compose bind mount shadows everything under /usr/src/app.
COPY --chmod=755 entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

# The compose bind mount makes the container's uid the owner of anything written
# back to the host, and manage.py writes real files (migrations, __pycache__).
# Matching the host uid keeps those files editable outside Docker.
RUN useradd --create-home --uid 1000 app
USER app

# uvicorn, not runserver: the same ASGI runtime prod runs, so a sync-only or
# thread-local surprise shows up here first. --reload polls for changes, which
# is what works through a Windows bind mount. /static/ comes from WhiteNoise's
# finders (on when DEBUG). --no-access-log: apps.commons.middleware logs instead.
CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload", "--no-access-log"]

# ---------------------------------------------------------------- builder
FROM base AS builder

# --locked: fail if uv.lock is stale against pyproject.toml, instead of silently
#           building yesterday's dependencies.
# --no-dev: leave pytest out of the runtime image.
# --compile-bytecode: pay .pyc compilation at build time, not on the first request.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --compile-bytecode

FROM python:3.14-slim-bookworm AS prod

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"
ENV DJANGO_SETTINGS_MODULE=config.settings.production

COPY --from=builder /opt/venv /opt/venv

WORKDIR /usr/src/app
COPY . .

# collectstatic imports settings, which demand real credentials. These placeholders
# never reach the final image: nothing is read from them but the static config.
RUN SECRET_KEY=build ALLOWED_HOSTS=build POSTGRES_DB=build POSTGRES_USER=build POSTGRES_PASSWORD=build \
    POSTGRES_HOST=build POSTGRES_PORT=5432 REDIS_URL=redis://build:6379/0 \
    CORS_ALLOWED_ORIGINS=http://build BILLING_GO_LIVE=2000-01-01 \
    python manage.py collectstatic --noinput

# --chmod: the exec bit is set here rather than relying on the one git recorded,
# so a fresh clone on any platform builds a runnable entrypoint.
# ponytail: migrate runs per container, which assumes a single replica. With more
# than one they race -- move migrate to its own deploy step before scaling out.
COPY --chmod=755 entrypoint.sh /entrypoint.sh

RUN useradd --create-home --uid 1000 app
USER app

ENTRYPOINT ["/entrypoint.sh"]

# ASGI through gunicorn's UvicornWorker: gunicorn stays as the process manager
# for what uvicorn alone lacks -- --timeout kills a hung worker, --max-requests
# (with jitter) recycles them to bound memory growth.
#
# Under ASGI the worker count is no longer the concurrency limit: DRF's sync
# views run on a thread per request, and an async view waiting on I/O holds no
# thread at all -- which is what a long-lived event stream needs. So 3, down
# from the 5 the sync workers needed: concurrency now comes from threads, and
# each worker is a full copy of Django in memory on a box shared with Postgres
# and Redis. Raise it against measured RSS or CPU, not open tabs. Mind the DB pool in
# settings: workers x max_size has to stay under Postgres's max_connections.
#
# No --access-logfile: UvicornWorker would route uvicorn's access format, which
# has no duration, through it. apps.commons.middleware.access_log writes one
# line per request with the duration instead.
CMD ["gunicorn", "config.asgi:application", \
     "--worker-class", "uvicorn_worker.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "100"]
