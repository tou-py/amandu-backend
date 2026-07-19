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

# runserver only: autoreload, readable tracebacks, and it serves /static/ itself.
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

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
    AWS_S3_ENDPOINT_URL=http://build AWS_ACCESS_KEY_ID=build \
    AWS_SECRET_ACCESS_KEY=build AWS_STORAGE_BUCKET_NAME=build \
    python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 app
USER app

# --timeout kills hung workers, --max-requests recycles them to bound memory growth.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--timeout", "60", \
     "--max-requests", "1000", \
     "--max-requests-jitter", "100", \
     "--access-logfile", "-"]
