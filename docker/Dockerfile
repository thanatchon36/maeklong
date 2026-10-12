# riverbank bot image (SPEC §8). Build from the repo root:
#   docker compose -f docker/docker-compose.yml build
# The image holds only the installed package. Config, data, state, reports, logs and the KILL file live in
# the bind-mounted working directory /work (the repo checkout by default), so host tools see the same files.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    MPLCONFIGDIR=/tmp/matplotlib

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

# Runs as the uid/gid given by compose (`user:`), so files written to the bind mount belong to the operator.
WORKDIR /work
CMD ["python", "-m", "riverbank.main", "--config", "config/config.yaml"]
