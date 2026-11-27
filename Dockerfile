FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /app
COPY Dockerfile .dockerignore pyproject.toml uv.lock README.md LICENSE ./
COPY afterset ./afterset
RUN tar -czf /tmp/afterset-source.tar.gz Dockerfile .dockerignore pyproject.toml uv.lock README.md LICENSE afterset
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
RUN useradd --create-home --uid 10001 afterset && mkdir /data && chown afterset:afterset /data
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /tmp/afterset-source.tar.gz /usr/share/doc/afterset/source.tar.gz
COPY LICENSE /usr/share/doc/afterset/LICENSE
LABEL org.opencontainers.image.title="Afterset" \
    org.opencontainers.image.description="Personal Garmin training, sleep and recovery MCP server" \
    org.opencontainers.image.version="0.4.1" \
    org.opencontainers.image.licenses="AGPL-3.0-or-later"
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    AFTERSET_DB=/data/afterset.db \
    AFTERSET_TOKEN_STORE=/data/garmin_tokens \
    AFTERSET_HOST=0.0.0.0 \
    PORT=8080
USER afterset
WORKDIR /home/afterset
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('AFTERSET_PORT',os.environ.get('PORT','8080'))+'/healthz',timeout=3).close()"]
CMD ["afterset-mcp", "--transport", "streamable-http"]
