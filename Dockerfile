# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.9.13 AS uv

FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install --no-install-recommends --yes libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /bin/

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-default-groups --no-editable \
    && mkdir -p /app/data \
    && useradd --create-home --uid 10001 sift \
    && chown -R sift:sift /app/data

USER sift

EXPOSE 8000

CMD ["uvicorn", "sift.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
