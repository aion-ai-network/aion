FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /bin/uv
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-install-project
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev
RUN useradd --create-home --uid 10001 aion && mkdir -p /data/private && chown aion:aion /data/private
USER aion
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "aion.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
