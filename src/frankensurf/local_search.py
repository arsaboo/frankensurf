"""A local search engine for FrankenSurf: SearXNG on loopback, with or without Docker.

FrankenSurf's search router already prefers a SearXNG at http://127.0.0.1:8088 and
skips it when nothing answers there. This module is how an installed package (no
repo scripts) runs that engine, two ways:

- native (`frankensurf install search`): SearXNG's source at one pinned commit,
  checked against its SHA-256, in its own virtual environment, run by FrankenSurf
  as a background process. No Docker. Search starts it again when it is not running.
- Docker (`frankensurf search-engine start` when native is not installed): one
  pinned image, one named container.

Both use the same settings file, shipped as package data. No API key; the engine's
own upstream searches are the only traffic that leaves the machine.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
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
INSTALL_COMMAND = "frankensurf install search"
DOCKER_HELP = "Install Docker (https://docs.docker.com/get-docker/), start it, then run: " + START_COMMAND
INSTALL_HELP = f"Run: {INSTALL_COMMAND} (no Docker needed)"

# Native: SearXNG's source at one commit, refused unless it matches this SHA-256.
# To move the pin: download the new archive once, hash it, check it runs, change both.
NATIVE_COMMIT = "ad46ebd7d87666edb5f116c5b274b47f689ac6cd"
NATIVE_SHA256 = "871fe907392870a72fe4f49dc0aa1a92d7c56df123c0fb132d77e6cf682a95eb"
NATIVE_ARCHIVE = f"https://github.com/searxng/searxng/archive/{NATIVE_COMMIT}.tar.gz"
# Set to 0 and search never starts the native engine on its own.
AUTOSTART_ENV = "FRANKENSURF_SEARCH_AUTOSTART"
FALLBACK = ("Search still works without it: the free sources (DuckDuckGo, Bing and the topic-specific "
            "ones) answer, but results are thinner")
NOT_RUNNING_NOTE = ("Local search (SearXNG) was not running, so this search used other sources "
                    "and may be thinner. Start it with: " + START_COMMAND)
NOT_INSTALLED_NOTE = ("No local search engine (SearXNG) answered, so this search used other sources "
                      "and may be thinner. Install one (no Docker needed): frankensurf install search; or, "
                      "if you run it in Docker: " + START_COMMAND)

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
    return {"status": "no_docker", "url": url, "how_to_start": INSTALL_HELP, "or_with_docker": DOCKER_HELP,
            "without_it": FALLBACK,
            "docker": "not installed" if problem == "missing" else "installed but not running"}


def _occupied(url, state, port):
    return _not_running(url, error=(
        f"Port {port} is answered by something that is not a JSON-enabled SearXNG "
        f"({'JSON output is off' if state == 'json_disabled' else 'not SearXNG'}); free the port, or "
        "point FrankenSurf at another SearXNG with FRANKENSURF_SEARCH_URL"))


# --- native: SearXNG without Docker ---------------------------------------------------------

def native_dir():
    return config_dir().parent / "searxng-native"


def _native_python(root=None):
    venv = Path(root or native_dir()) / "venv"
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def native_installed():
    """True when the pinned native SearXNG is installed and complete."""
    root = native_dir()
    try:
        marker = json.loads((root / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (marker.get("commit") == NATIVE_COMMIT and _native_python(root).is_file()
            and (root / "src" / "searx" / "webapp.py").is_file())


def _download(url, destination):
    """Stream url into an open binary file (redirects followed: GitHub archives redirect)."""
    with httpx.Client(transport=_TRANSPORT, timeout=httpx.Timeout(30.0, read=120.0),
                      follow_redirects=True) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1 << 16):
                destination.write(chunk)
    destination.flush()


def install_native(log=print):
    """Install SearXNG without Docker: the pinned source archive (refused unless its
    SHA-256 matches), unpacked into FrankenSurf's data folder, with its Python packages
    in a virtual environment of its own. Safe to run again: an install of the same
    commit is kept. Raises on failure; nothing half-installed is marked installed."""
    import venv
    from .browser_install import verify_sha256
    root = native_dir()
    if native_installed():
        return {"status": "installed", "already": True, "path": str(root), "commit": NATIVE_COMMIT}
    root.mkdir(parents=True, exist_ok=True)
    (root / "installed.json").unlink(missing_ok=True)
    log("Downloading SearXNG (pinned source, about 7 MB)")
    with tempfile.TemporaryFile() as archive, tempfile.TemporaryDirectory(dir=root) as unpack:
        _download(NATIVE_ARCHIVE, archive)
        verify_sha256(archive, NATIVE_SHA256, NATIVE_ARCHIVE)
        with tarfile.open(fileobj=archive, mode="r:gz") as bundle:
            bundle.extractall(unpack, filter="data")
        tops = [entry for entry in Path(unpack).iterdir() if entry.is_dir()]
        if len(tops) != 1 or not (tops[0] / "searx" / "webapp.py").is_file():
            raise RuntimeError("the SearXNG archive does not have the expected layout")
        shutil.rmtree(root / "src", ignore_errors=True)
        tops[0].rename(root / "src")
    log("Installing SearXNG's Python packages (about 130 MB, in its own environment)")
    venv.EnvBuilder(with_pip=True, clear=True).create(root / "venv")
    subprocess.run([str(_native_python(root)), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                    "-r", str(root / "src" / "requirements.txt")], stdout=sys.stderr, check=True, timeout=1800)
    (root / "installed.json").write_text(json.dumps({"commit": NATIVE_COMMIT, "sha256": NATIVE_SHA256}),
                                         encoding="utf-8")
    return {"status": "installed", "path": str(root), "commit": NATIVE_COMMIT}


def uninstall_native():
    """Stop the native engine and remove it (the settings file, yours, is kept)."""
    stop_native()
    root = native_dir()
    existed = root.exists()
    shutil.rmtree(root, ignore_errors=True)
    return {"removed": str(root) if existed else None}


def _pid_file():
    return native_dir() / "searxng.pid"


def native_pid():
    """The running native engine's process id, or None. A stale pid file (the process
    gone, or its number reused by something else) counts as not running."""
    try:
        pid = int(_pid_file().read_text().strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        try:
            command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True,
                                     text=True, check=False, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    return pid if "searx.webapp" in command else None


def start_native(*, url=URL, port=PORT, folder=None, wait=30):
    """Run the installed native engine in the background on loopback. It outlives this
    command, like the Docker container; `frankensurf search-engine stop` stops it."""
    root = native_dir()
    settings = prepare_settings(folder) / "settings.yml"
    env = {**os.environ, "SEARXNG_SETTINGS_PATH": str(settings), "SEARXNG_PORT": str(port),
           "SEARXNG_BIND_ADDRESS": HOST, "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    with open(root / "searxng.log", "ab") as log:
        process = subprocess.Popen([str(_native_python(root)), "-m", "searx.webapp"], cwd=root / "src",
                                   env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                   start_new_session=True)
    _pid_file().write_text(str(process.pid))
    if not _wait_running(url, wait):
        if process.poll() is not None:
            _pid_file().unlink(missing_ok=True)
        return _not_running(url, engine="native", error=(
            f"the native engine did not answer on {url} within {wait}s (log: {root / 'searxng.log'})"))
    return {"status": "started", "url": url, "engine": "native", "pid": process.pid}


def stop_native():
    """Stop the native engine if it runs; True when it did."""
    pid = native_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
    _pid_file().unlink(missing_ok=True)
    return True


_autostarted = False


def autostart(url=URL):
    """Search calls this when the default engine did not answer: if the native engine is
    installed, start it, once per process. True when it answers afterwards."""
    global _autostarted
    if (_autostarted or url not in (None, URL) or os.environ.get(AUTOSTART_ENV) == "0"
            or not native_installed()):
        return False
    _autostarted = True
    state = probe(URL)
    if state != "unreachable":
        return state == "running"
    return start_native(wait=15).get("status") == "started"


def status(url=URL):
    """Report only; starts nothing. Docker is asked only when nothing answers."""
    state = probe(url)
    if state == "running":
        return {"status": "running", "url": url, **({"engine": "native"} if native_pid() else {})}
    if state != "unreachable":
        return _occupied(url, state, PORT)
    if native_installed():
        return _not_running(url, engine="native")
    problem = docker_problem()
    return _no_docker(url, problem) if problem else _not_running(url)


def start(*, url=URL, name=CONTAINER, port=PORT, folder=None, wait=90):
    """Make a local SearXNG answer at url: the native engine when it is installed, else
    Docker. Idempotent: if one already answers, nothing is run."""
    state = probe(url)
    if state == "running":
        return {"status": "running", "url": url}
    if state != "unreachable":
        return _occupied(url, state, port)
    if native_installed():
        return start_native(url=url, port=port, folder=folder)
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
    """Stop FrankenSurf's own engine: the native process, or the container (kept; start
    brings it back)."""
    if stop_native():
        return _not_running(url, note="stopped the native engine")
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
    if native_installed():
        return {"status": "not_running", "note": NOT_RUNNING_NOTE, "start_with": START_COMMAND}
    return {"status": "not_installed", "note": NOT_INSTALLED_NOTE, "install_with": INSTALL_COMMAND}
