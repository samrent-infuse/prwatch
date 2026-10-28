FROM node:22-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 git gh ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN npm install -g @anthropic-ai/claude-code@2.1.308 && npm cache clean --force

# node:22 ships a uid-1000 "node" user; run as it (claude refuses some modes as root).
RUN mkdir -p /data /repo && chown node:node /data /repo
USER node
ENV HOME=/home/node PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=node:node entrypoint.sh ./
COPY --chown=node:node prompts/ ./prompts/
COPY --chown=node:node prwatch/ ./prwatch/
COPY --chown=node:node tests/ ./tests/
ENTRYPOINT ["sh", "/app/entrypoint.sh"]
