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

CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---------------------------------------------------------------- builder
FROM base AS builder

# --locked: fail if uv.lock is stale against pyproject.toml, instead of silently
#           building yesterday's dependencies.
# --no-dev: leave pytest and debug-toolbar out of the runtime image.
# --compile-bytecode: pay .pyc compilation at build time, not on the first request.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --compile-bytecode

FROM python:3.14-slim-bookworm AS prod

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv

WORKDIR /usr/src/app
COPY . .

RUN useradd --create-home --uid 1000 app
USER app

CMD ["uvicorn", "config.asgi:application", "--host", "0.0.0.0", "--port", "8000"]
