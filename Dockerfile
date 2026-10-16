# Frankensurf in a container: one install that every machine on a network can use.
#
#   docker run -d -p 3002:3002 -e FRANKENSURF_API_KEY=change-me -v frankensurf:/data --shm-size=1g \
#       ghcr.io/yail259/frankensurf
#
# Default: the Firecrawl-compatible API on :3002 (frankensurf serve). For MCP over the network:
#   docker run ... -p 8000:8000 ghcr.io/yail259/frankensurf frankensurf-mcp --http --host 0.0.0.0
# Both refuse to start on the network without FRANKENSURF_API_KEY (or --allow-unauthenticated).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    HOME=/home/frankensurf \
    FRANKENSURF_STATE=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --home-dir /home/frankensurf --uid 10001 frankensurf \
 && mkdir -p /data /ms-playwright

WORKDIR /opt/frankensurf
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[mcp]"

# Chromium and its system libraries (and Firefox's, for Camoufox), then the free stealth browsers in
# their own environment. The stealth browsers aren't built for every platform: where one fails, the
# image still works with plain HTTP, Jina Reader and Chromium.
RUN frankensurf install chromium --with-deps \
 && (frankensurf install stealth || echo "Some stealth browsers aren't available on this platform") \
 && rm -rf /var/lib/apt/lists/* /home/frankensurf/.cache/pip \
 && chown -R frankensurf:frankensurf /home/frankensurf /ms-playwright /data

USER frankensurf
WORKDIR /home/frankensurf
VOLUME ["/data"]
EXPOSE 3002 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:3002/health || curl -fsS -o /dev/null http://127.0.0.1:8000/mcp || exit 1
ENTRYPOINT ["tini", "--"]
CMD ["frankensurf", "serve", "--host", "0.0.0.0"]
