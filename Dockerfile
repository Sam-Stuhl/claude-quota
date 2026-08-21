# claude-quota daemon, for running the server centrally (e.g. via console).
# Clients point their statusline + OTel at it; this container collects, stores,
# and serves the dashboard. Set CLAUDE_QUOTA_TOKEN to require a shared secret on
# ingest, and mount a volume at /data to persist the SQLite database.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV CLAUDE_QUOTA_HOST=0.0.0.0 \
    CLAUDE_QUOTA_DIR=/data

# 7788 = HTTP API + web + statusline ingest; 4318 = OTLP/HTTP; 4317 = OTLP/gRPC.
EXPOSE 7788 4318 4317
VOLUME ["/data"]

CMD ["claude-quotad"]
