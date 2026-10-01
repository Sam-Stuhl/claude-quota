# claude-quota daemon, for running the server centrally (e.g. via console).
# Clients point their statusline + OTel at it; this container collects, stores,
# and serves the dashboard. Set CLAUDE_QUOTA_TOKEN to require a shared secret on
# ingest, and mount a volume at /data to persist the SQLite database.
FROM python:3.12-slim

WORKDIR /app
# Dependencies first, keyed on pyproject.toml alone, so a source change reuses
# this layer instead of reinstalling numpy, scipy and grpcio. Include the
# Postgres backend: a container has no persistent volume, so it keeps its data
# in an external database via DATABASE_URL.
COPY pyproject.toml README.md ./
RUN python -c "import tomllib; p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
print('\n'.join(p['dependencies'] + p['optional-dependencies']['postgres']))" > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt
COPY src ./src
RUN pip install --no-cache-dir --no-deps .

ENV CLAUDE_QUOTA_HOST=0.0.0.0 \
    CLAUDE_QUOTA_DIR=/data

# 7788 = HTTP API + web + statusline ingest; 4318 = OTLP/HTTP; 4317 = OTLP/gRPC.
EXPOSE 7788 4318 4317
VOLUME ["/data"]

CMD ["claude-quotad"]
