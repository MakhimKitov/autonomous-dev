# The control plane as an OCI image (TO-BE §5, E5): one stateless replica per
# container, N of them over one ledger. Everything a replica is comes in through
# 12-factor env — LEDGER_DSN, ARCHIVE_URL, the App refs, WAKE_BIND — and through
# `--data-dir`, which holds only this process's own scratch (volumes, logs, the
# platform clone).
#
# Bursts still run through `DockerRunner` (the Jobs runner is Stage G), so the
# image carries the docker *CLI* and expects the host's daemon socket mounted
# at /var/run/docker.sock. That daemon reads bind-mount paths in *its* namespace,
# so the data dir must be mounted at the same absolute path inside and out —
# docker/compose.yaml's `platform` profile does exactly that.
#
# Build (repo root): docker build -f docker/control-plane/Dockerfile -t autonomous-dev/control-plane:latest .

FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.9.21 /uv /usr/local/bin/uv

# The workspace, resolved from the lockfile: control-plane and the two leaf
# packages it depends on (dev-agent for the env contract, secret-refs for the
# refs), nothing editable — the venv is the artifact, the sources stay here.
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY packages/ packages/
RUN uv sync --locked --no-dev --no-editable --package control-plane


FROM python:3.12-slim

# git: the platform-owned clone (SCHEDULER.md, "The clone").
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# The docker CLI alone — no daemon, no dind: `docker run` here talks to the
# socket the deployment mounts. Static binary, pinned.
ARG DOCKER_CLI_VERSION=28.3.3
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in arm64) target="aarch64" ;; amd64) target="x86_64" ;; *) echo "unsupported arch: $arch" && exit 1 ;; esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${target}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker \
    && docker --version

COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Root, on purpose and for now: the daemon socket a deployment mounts is
# root-owned on most hosts. The replica holds the App keys as refs either way.
WORKDIR /srv/autonomous-dev
ENTRYPOINT ["control-plane"]
CMD ["run", "--help"]
