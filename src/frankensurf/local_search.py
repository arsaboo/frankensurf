"""A local search engine for FrankenSurf: SearXNG in Docker, loopback only.

FrankenSurf's search router already prefers a SearXNG at http://127.0.0.1:8088 and
skips it when nothing answers there. This module is how an installed package (no
repo scripts) starts that engine: one pinned image, one named container, settings
shipped as package data. No API key; the container's own upstream searches are the
only traffic that leaves the machine.
"""
from __future__ import annotations

import secrets
import subprocess
import sys
import time
from pathlib import Path

import httpx

# Pinned by digest: the same image scripts/local_services.py runs.
IMAGE = ("ghcr.io/searxng/searxng@"
         "sha256:a07a5cd2da2c63d66e559f9e4d3a3db106cfc6c32fb0ac70abe91cc28bcd7350")
# The same container name as scripts/local_services.py, so the two never fight over port 8088.
CONTAINER = "frankensurf-search"
HOST = "127.0.0.1"
PORT = 8088
URL = f"http://{HOST}:{PORT}"
MEMORY = "768m"
START_COMMAND = "frankensurf search-engine start"
DOCKER_HELP = "Install Docker (https://docs.docker.com/get-docker/), start it, then run: " + START_COMMAND
FALLBACK = ("Search still works without it: the free sources (DuckDuckGo, Bing and the topic-specific "
            "ones) answer, but results are thinner")
NOT_RUNNING_NOTE = ("Local search (SearXNG) was not running, so this search used other sources "
                    "and may be thinner. Start it with: " + START_COMMAND)

_TRANSPORT = None   # tests inject an httpx.MockTransport


class DockerUnavailable(Exception):
    """No usable docker: the CLI is missing, or its daemon is not running."""

    def __init__(self, reason, detail=""):
        super().__init__(detail or reason)
        self.reason = reason     # "missing" or "daemon"


def _docker(*args, timeout=60, stream=False):
    """Run the docker CLI. stream=True sends its progress to stderr (stdout stays JSON)."""
    try:
        if stream:
            return subprocess.run(["docker", *args], stdout=sys.stderr, stderr=sys.stderr,
                                  text=True, timeout=timeout, check=False)
        return subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise DockerUnavailable("missing") from error
    except subprocess.TimeoutExpired as error:
        raise DockerUnavailable("daemon", "docker did not answer in time") from error


def docker_problem():
    """None when docker works, else "missing" (no CLI) or "daemon" (a CLI with no running engine)."""
    try:
        result = _docker("version", "--format", "{{.Server.Version}}", timeout=15)
    except DockerUnavailable as error:
        return error.reason
    return None if result.returncode == 0 else "daemon"


def probe(url=URL, *, timeout=3.0):
    """What answers at url: "running" (SearXNG with JSON on), "json_disabled" (SearXNG,
    JSON refused), "other" (something else) or "unreachable". The query is empty, so the
    probe never triggers a real upstream search."""
    try:
        with httpx.Client(transport=_TRANSPORT, timeout=timeout, trust_env=False) as client:
            response = client.get(url.rstrip("/") + "/search", params={"q": "", "format": "json"})
    except httpx.HTTPError:
        return "unreachable"
    if response.status_code == 403:
        return "json_disabled"
    if response.status_code in (200, 400):
        try:
            body = response.json()
        except ValueError:
            return "other"
        # SearXNG with JSON on answers an empty query {"error": "No query"} (400).
        if isinstance(body, dict) and (body.get("error") == "No query"
                                       or isinstance(body.get("results"), list)):
            return "running"
    return "other"


def config_dir():
    return Path.home() / ".local" / "share" / "frankensurf" / "searxng"


def settings_template():
    return Path(__file__).with_name("searxng_settings.yml").read_text(encoding="utf-8")


def prepare_settings(folder=None):
    """The folder mounted at /etc/searxng. Written once with a random secret key; an
    existing settings file is the user's and is never overwritten."""
    folder = Path(folder) if folder else config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    settings = folder / "settings.yml"
    if not settings.exists():
        settings.write_text(settings_template().replace("__SECRET_KEY__", secrets.token_hex(32)),
                            encoding="utf-8")
        try:
            settings.chmod(0o600)
        except OSError:
            pass
    return folder


def run_command(folder, *, name=CONTAINER, port=PORT):
    return ["run", "-d", "--name", name, "--restart", "unless-stopped", f"--memory={MEMORY}",
            "-p", f"{HOST}:{port}:8080", "-v", f"{folder}:/etc/searxng", IMAGE]


def _container_state(name):
    """"running", "exited", ... or None when there is no such container."""
    result = _docker("inspect", "--format", "{{.State.Status}}", name)
    return (result.stdout or "").strip() or None if result.returncode == 0 else None


def _wait_running(url, seconds):
    deadline = time.monotonic() + seconds
    while True:
        if probe(url) == "running":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _not_running(url, **extra):
    return {"status": "not_running", "url": url, "how_to_start": START_COMMAND, **extra}


def _no_docker(url, problem):
    return {"status": "no_docker", "url": url, "how_to_start": DOCKER_HELP, "without_it": FALLBACK,
            "docker": "not installed" if problem == "missing" else "installed but not running"}


def _occupied(url, state, port):
    return _not_running(url, error=(
        f"Port {port} is answered by something that is not a JSON-enabled SearXNG "
        f"({'JSON output is off' if state == 'json_disabled' else 'not SearXNG'}); free the port, or "
        "point FrankenSurf at another SearXNG with FRANKENSURF_SEARCH_URL"))


def status(url=URL):
    """Report only; starts nothing. Docker is asked only when nothing answers."""
    state = probe(url)
    if state == "running":
        return {"status": "running", "url": url}
    if state != "unreachable":
        return _occupied(url, state, PORT)
    problem = docker_problem()
    return _no_docker(url, problem) if problem else _not_running(url)


def start(*, url=URL, name=CONTAINER, port=PORT, folder=None, wait=90):
    """Make a local SearXNG answer at url. Idempotent: if one already does, nothing is run."""
    state = probe(url)
    if state == "running":
        return {"status": "running", "url": url}
    if state != "unreachable":
        return _occupied(url, state, port)
    problem = docker_problem()
    if problem:
        return _no_docker(url, problem)
    try:
        existing = _container_state(name)
        if existing is None:
            if _docker("image", "inspect", IMAGE).returncode != 0:
                print("Pulling the SearXNG image (about 100 MB, once)", file=sys.stderr, flush=True)
                if _docker("pull", IMAGE, timeout=900, stream=True).returncode != 0:
                    return _not_running(url, error="docker could not pull the SearXNG image (offline?)")
            created = _docker(*run_command(prepare_settings(folder), name=name, port=port))
            if created.returncode != 0:
                return _not_running(url, error=(created.stderr or created.stdout or "").strip()[-300:])
        elif existing != "running":
            started = _docker("start", name)
            if started.returncode != 0:
                return _not_running(url, error=(started.stderr or started.stdout or "").strip()[-300:])
    except (DockerUnavailable, OSError) as error:
        return _not_running(url, error=str(error)[:300])
    if not _wait_running(url, wait):
        return _not_running(url, error=f"container {name} started but did not answer on {url} "
                                       f"within {wait}s (docker logs {name})")
    return {"status": "started", "url": url, "container": name, "image": IMAGE}


def stop(*, name=CONTAINER, url=URL):
    """Stop FrankenSurf's own container (it is kept; start brings it back)."""
    try:
        if _container_state(name) != "running":
            return _not_running(url, note=f"no running container named {name}")
        stopped = _docker("stop", name)
    except DockerUnavailable as error:
        return _no_docker(url, error.reason)
    if stopped.returncode != 0:
        return {"status": "running", "url": url, "error": (stopped.stderr or "").strip()[-300:]}
    return _not_running(url, note=f"stopped {name}")


def routing_note():
    """What the search receipt says when the local engine did not answer."""
    return {"status": "not_running", "note": NOT_RUNNING_NOTE, "start_with": START_COMMAND}
