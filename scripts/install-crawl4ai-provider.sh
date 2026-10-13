#!/usr/bin/env bash
# From a clone: the installer ships inside the package (frankensurf install crawl4ai runs it too).
exec bash "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)/src/frankensurf/install_crawl4ai.sh" "$@"
