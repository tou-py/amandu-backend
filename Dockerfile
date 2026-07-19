FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

WORKDIR /usr/src/app

# set environment variables

# PYTHONDONTWRITEBYTECODE=1: Prevents Python from writing .pyc files, reducing unnecessary disk usage.
# PYTHONUNBUFFERED=1: Ensures that the Python output (e.g., print statements) is immediately flushed to the terminal/logs, making debugging easier.
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/usr/src/app/.venv/bin:$PATH"
# UV_LINK_MODE=copy: the uv cache lives on a separate mount, so hardlinking is impossible.
ENV UV_LINK_MODE=copy

# Only the lockfile inputs, so this layer is reused whenever dependencies are unchanged.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

COPY . .

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
