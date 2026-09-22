FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl fontconfig fonts-noto-core fonts-noto-mono fonts-roboto-slab libpango-1.0-0 librsvg2-bin \
    && rm -rf /var/lib/apt/lists/*

ENV UV_CACHE_DIR=/tmp/uv-cache
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY alembic ./alembic
COPY src ./src
COPY main.py ./
RUN uv sync --frozen --no-dev
RUN .venv/bin/playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

RUN groupadd --system merch && useradd --system --gid merch --home /app merch \
    && mkdir -p /app/.data /app/.cache/fontconfig \
    && chown -R merch:merch /app/.data /app/.cache
USER merch

EXPOSE 8000
CMD [".venv/bin/merch", "web"]
