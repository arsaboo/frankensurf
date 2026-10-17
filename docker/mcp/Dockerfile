# Frankensurf as a stdio MCP server, for Docker's MCP Catalog and Toolkit (docker/mcp-registry).
# Built from the repo root:  docker build -f docker/mcp/Dockerfile -t frankensurf-mcp .
# The same install as ../../Dockerfile with frankensurf-mcp as its command. For MCP over the
# network, or the Firecrawl-compatible API, use ghcr.io/yail259/frankensurf (see /docs/docker/).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/frankensurf \
    FRANKENSURF_STATE=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --home-dir /home/frankensurf --uid 10001 frankensurf \
 && mkdir -p /data /ms-playwright

WORKDIR /opt/frankensurf
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[mcp]"

RUN frankensurf install chromium --with-deps \
 && (frankensurf install stealth || echo "Some stealth browsers aren't available on this platform") \
 && rm -rf /var/lib/apt/lists/* /home/frankensurf/.cache/pip \
 && chown -R frankensurf:frankensurf /home/frankensurf /ms-playwright /data

USER frankensurf
WORKDIR /home/frankensurf
ENTRYPOINT ["tini", "--"]
CMD ["frankensurf-mcp"]
