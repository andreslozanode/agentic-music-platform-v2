# syntax=docker/dockerfile:1.10
# One hardened image recipe for every agent:
#   docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=autonomous-agent -t agentic-api .
#   docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=music-agent      -t music-mcp .
#   docker build -f deploy/docker/Dockerfile --build-arg PACKAGE=reddit-agent     -t reddit-agent .
# Base images are pinned by tag here; CI resolves and records digests (SBOM + provenance),
# and Dependabot (docker ecosystem) proposes updates.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.7

FROM ${UV_IMAGE} AS uv

# ---------------------------------------------------------------- builder
FROM ${PYTHON_IMAGE} AS builder
ARG PACKAGE
RUN test -n "${PACKAGE}" || (echo "PACKAGE build-arg is required" && exit 1)
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src

# Dependency layer (cached until the lockfile changes)
COPY pyproject.toml uv.lock ./
COPY packages/agent-core/pyproject.toml packages/agent-core/pyproject.toml
COPY agents/level1-reddit-agent/pyproject.toml agents/level1-reddit-agent/pyproject.toml
COPY agents/level2-music-agent/pyproject.toml agents/level2-music-agent/pyproject.toml
COPY agents/level3-autonomous-agent/pyproject.toml agents/level3-autonomous-agent/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${PACKAGE}"

# Application layer: installed as regular (non-editable) wheels, source is not shipped
COPY packages packages
COPY agents agents
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${PACKAGE}" \
 && find /opt/venv -name "*.pyc" -path "*tests*" -delete

# ---------------------------------------------------------------- runtime
FROM ${PYTHON_IMAGE} AS runtime
ARG PACKAGE
ARG VERSION=0.0.0-dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="${PACKAGE}" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.source="https://github.com/agentic-ai-platform/agentic-ai-platform" \
      org.opencontainers.image.licenses="Apache-2.0"

# Patch OS packages, drop pip/setuptools (not needed at runtime), create an unprivileged user.
RUN apt-get update \
 && apt-get upgrade -y --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y pip setuptools wheel || true \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /app /var/lib/agentic \
 && chown app:app /var/lib/agentic

COPY --from=builder --chown=root:root /opt/venv /opt/venv
RUN ln -s "/opt/venv/bin/${PACKAGE}" /usr/local/bin/app

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    HOME=/tmp \
    AGENT_STATE_DIR=/var/lib/agentic/state \
    AGENT_STORAGE_ROOT=/var/lib/agentic/lakehouse \
    AGENT_QDRANT_PATH=/var/lib/agentic/qdrant

WORKDIR /app
USER 10001:10001
EXPOSE 8080 9464
# Kubernetes probes are used instead of Docker HEALTHCHECK.
HEALTHCHECK NONE
ENTRYPOINT ["/usr/local/bin/app"]
CMD ["--help"]
