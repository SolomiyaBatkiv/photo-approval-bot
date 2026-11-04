# syntax=docker/dockerfile:1

# ---- build: ставимо залежності у venv через uv ----
FROM python:3.13-alpine AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# ---- runtime: тільки python + venv + код ----
FROM python:3.13-alpine
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY bot.py ./
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/bot.db
EXPOSE 8080
CMD ["python", "bot.py"]
