# Dev-agent warm base image (ARCHITECTURE.md §6): toolchain + claude CLI +
# the burst worker. A burst is then just clone/checkout + agent boot.
#
# Walls baked into the image (§8):
#   - only the dev-agent package is installed — no control-plane code in the box
#   - non-root user; no credentials of any kind in the image
#   - CLAUDE_CONFIG_DIR is the /session mount the control plane owns
#
# Build (repo root): docker build -f docker/dev-agent/Dockerfile -t autonomous-dev/dev-agent:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Build the dev-agent wheel in isolation — nothing else from the repo enters the image.
COPY packages/dev-agent /src/dev-agent
RUN uv build --wheel /src/dev-agent --out-dir /wheels


FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 agent \
    && mkdir -p /session /workspace/repo \
    && chown -R agent:agent /session /workspace

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER agent
ENV HOME=/home/agent \
    PATH="/home/agent/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR=/session

# Claude Code CLI — the engine the SDK drives. Native standalone install.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && claude --version

WORKDIR /workspace/repo
VOLUME ["/session", "/workspace/repo"]

ENTRYPOINT ["dev-agent"]
CMD ["run"]
