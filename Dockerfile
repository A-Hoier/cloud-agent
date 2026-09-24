# syntax=docker/dockerfile:1

# Node runs the optional upstream DeepSeek Harness and Copilot CLI adapters.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.source="https://github.com/A-Hoier/cloud-agent" \
      org.opencontainers.image.description="Isolated coding agent for Azure Container Apps"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    HOME=/home/agent \
    WORKDIR=/tmp/coding-agent

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep ca-certificates openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY tools/package.json tools/package-lock.json /opt/cli/
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm ci --omit=dev --prefix /opt/cli --no-audit --no-fund \
    && ln -s /opt/cli/node_modules/.bin/copilot /usr/local/bin/copilot \
    && ln -s /opt/cli/node_modules/.bin/dsh /usr/local/bin/dsh \
    && npm cache clean --force

WORKDIR /app

COPY pyproject.toml README.md CHANGELOG.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY config ./config

RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /tmp/coding-agent \
    && chown -R agent:agent /app /tmp/coding-agent /home/agent \
    && chmod 0700 /tmp/coding-agent /home/agent
USER agent

CMD ["cloud-agent-worker"]
