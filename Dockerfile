FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY sdk ./sdk
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --all-packages

FROM build AS test
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --all-packages
COPY tests ./tests
COPY scripts ./scripts
COPY config ./config
COPY examples ./examples
COPY migrations ./migrations
COPY alembic.ini ./
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "scripts/run_tests.py"]

FROM python:3.12-slim AS runtime
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --no-create-home gateway
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY config ./config
COPY examples ./examples
COPY migrations ./migrations
COPY alembic.ini ./
RUN mkdir -p /app/var/spool && chown -R gateway:gateway /app/var
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"
CMD ["uvicorn", "llm_gateway_api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "30"]
