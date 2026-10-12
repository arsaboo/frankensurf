"""frankensurf install: every free tool FrankenSurf uses, in one command.

Like `playwright install` for browsers. `frankensurf install` fetches all of them,
`frankensurf install search chromium` only those, `--with-deps` adds the system
libraries the browsers need (Linux, through sudo, the way Playwright does it),
`--list` says what is installed where, and `frankensurf uninstall` removes what
install put there. Each tool lands in FrankenSurf's own folders or Playwright's
browser cache, never in your Python environment, and a tool that fails to install
leaves the others working.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

COMPONENTS = {
    "chromium": "Chromium for JavaScript pages (Playwright's build)",
    "stealth": "Stealth browsers: Camoufox, Scrapling and Patchright, in their own environment",
    "search": "Local search engine: SearXNG on 127.0.0.1:8088, run by FrankenSurf (no Docker)",
    "crawl4ai": "Crawl4AI and its own browsers, in their own environment",
}
# Installed only when named: nodriver is AGPL-3.0 and drives your own Chrome.
OPTIONAL = {
    "nodriver": "nodriver (AGPL-3.0; drives your installed Chrome), into the stealth environment",
}
DEFAULT = tuple(COMPONENTS)


def resolve(names):
    """The components to act on: none named (or "all") means every default one."""
    names = [name.lower() for name in names or []]
    if not names or names == ["all"]:
        return list(DEFAULT)
    unknown = [name for name in names if name not in COMPONENTS and name not in OPTIONAL]
    if unknown:
        raise ValueError(f"unknown: {', '.join(unknown)}; choose from all, "
                         + ", ".join([*COMPONENTS, *OPTIONAL]))
    return list(dict.fromkeys(names))


def _run(command, **kwargs):
    """Run an installer step with its progress on stderr (stdout stays JSON)."""
    return subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, check=False, **kwargs).returncode


def _playwright(*args):
    return [sys.executable, "-m", "playwright", *args]


# --- each component --------------------------------------------------------------------------

def _install_chromium(log):
    from .browser_install import clear_quarantine
    log("Downloading Chromium for JavaScript pages")
    ok = _run(_playwright("install", "chromium")) == 0
    clear_quarantine(log=log)
    return {"ok": ok} if ok else {"ok": False, "error": "playwright install chromium failed (see above)"}


def _install_stealth(log):
    from .experimental import install_free_providers
    result = install_free_providers(log=log)
    return {"ok": not result.get("errors"), **result}


def _install_search(log):
    from . import local_search
    installed = local_search.install_native(log=log)
    log("Starting the local search engine")
    running = local_search.start()
    return {"ok": running.get("status") in ("running", "started"), "path": installed["path"],
            "engine": running}


def _install_crawl4ai(log):
    from . import crawl4ai_config
    if shutil.which("bash") is None:
        return {"ok": False, "error": "needs bash (on Windows, run FrankenSurf inside WSL)"}
    log("Installing Crawl4AI and its browsers (a few minutes)")
    script = Path(__file__).with_name("install_crawl4ai.sh")
    code = _run(["bash", str(script)], env={**os.environ, "FRANKENSURF_CRAWL4AI_BASE_PYTHON": sys.executable})
    ok = code == 0 and crawl4ai_config.installed()
    return {"ok": ok, "path": str(crawl4ai_config.environment_dir())} if ok else {
        "ok": False, "error": f"the Crawl4AI installer stopped (exit {code}; see above)"}


def _install_nodriver(log):
    from .experimental import installed, worker_python
    if not installed("patchright"):
        return {"ok": False, "error": "install the stealth environment first: frankensurf install stealth"}
    log("Installing nodriver into the stealth environment")
    ok = _run([str(worker_python()), "-m", "pip", "install", "--quiet", "nodriver"]) == 0
    return {"ok": ok and installed("nodriver")}


INSTALLERS = {"chromium": _install_chromium, "stealth": _install_stealth, "search": _install_search,
              "crawl4ai": _install_crawl4ai, "nodriver": _install_nodriver}


def _install_deps(log):
    """System libraries for Chromium and Camoufox (a Firefox): Linux only."""
    if not sys.platform.startswith("linux"):
        return "not needed on this system"
    log("Installing the browsers' system libraries (Playwright asks for sudo)")
    return _run(_playwright("install-deps", "chromium", "firefox")) == 0


def install(names=None, *, with_deps=False, log=print):
    """Install the named components (all by default). One failing never stops the rest."""
    chosen = resolve(names)
    result = {}
    if with_deps:
        result["system_libraries"] = _install_deps(log)
    done = {}
    for name in chosen:
        try:
            done[name] = INSTALLERS[name](log)
        except Exception as error:   # report it and carry on with the next tool
            done[name] = {"ok": False, "error": str(error)[:300]}
        if not done[name]["ok"]:
            log(f"  {name}: not installed: {done[name].get('error') or 'see above'}")
    result["installed"] = done
    result["ok"] = all(item["ok"] for item in done.values()) and result.get("system_libraries") is not False
    failed = [name for name, item in done.items() if not item["ok"]]
    if failed:
        result["retry_with"] = "frankensurf install " + " ".join(failed)
        if sys.platform.startswith("linux") and not with_deps:
            result["if_a_browser_will_not_start"] = "frankensurf install --with-deps " + " ".join(failed)
    result["check_with"] = "frankensurf install --list"
    return result


# --- what is installed -----------------------------------------------------------------------

def _chromium_status():
    """Where Playwright keeps this version's Chromium, and whether it is there."""
    try:
        out = subprocess.run(_playwright("install", "--dry-run", "chromium"), capture_output=True,
                             text=True, check=False, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return {"installed": False}
    places = re.findall(r"Install location:\s*(\S.*)", out)
    return {"installed": bool(places) and all(Path(place.strip()).exists() for place in places),
            "where": places[0].strip() if places else None}


def listing():
    """Every component: installed or not, and where."""
    from . import crawl4ai_config, local_search
    from .experimental import installed, worker_python
    stealth_root = worker_python().parent.parent
    engine = local_search.status()
    native = local_search.native_installed()
    rows = {
        "chromium": _chromium_status(),
        "stealth": {"installed": all(installed(tool) for tool in ("camoufox", "scrapling", "patchright")),
                    "tools": {tool: installed(tool) for tool in ("camoufox", "scrapling", "patchright")},
                    "where": str(stealth_root)},
        # Any SearXNG answering on the default port counts (a Docker one, say); native is ours.
        "search": {"installed": native or engine["status"] == "running", "native": native,
                   "where": str(local_search.native_dir()) if native else engine.get("url"), "engine": engine},
        "crawl4ai": {"installed": crawl4ai_config.installed(), "where": str(crawl4ai_config.environment_dir())},
        "nodriver": {"installed": installed("nodriver"), "optional": True},
    }
    for name, row in rows.items():
        row["about"] = COMPONENTS.get(name) or OPTIONAL[name]
    missing = [name for name in DEFAULT if not rows[name]["installed"]]
    return {"components": rows, "missing": missing,
            **({"install_with": "frankensurf install " + " ".join(missing)} if missing else {})}


# --- uninstall -------------------------------------------------------------------------------

def _owned(path):
    """Only FrankenSurf's own data folder is ever deleted: an environment chosen with
    FRANKENSURF_PROVIDER_PYTHON or FRANKENSURF_CRAWL4AI_DIR is yours, and is left alone."""
    own = (Path.home() / ".local" / "share" / "frankensurf").resolve()
    return Path(path).resolve().is_relative_to(own)


def _remove(path):
    path = Path(path)
    if not _owned(path):
        return {"left_alone": str(path), "why": "not in FrankenSurf's own folder (set by you)"}
    existed = path.exists()
    shutil.rmtree(path, ignore_errors=True)
    return str(path) if existed else None


def _uninstall_stealth():
    from .experimental import worker_python
    python = worker_python()
    if python.is_file() and _owned(python.parent.parent):
        # Their browsers live outside the environment: let each tool remove its own.
        for command in (["-m", "camoufox", "remove"], ["-m", "patchright", "uninstall"]):
            subprocess.run([str(python), *command], stdout=sys.stderr, stderr=sys.stderr, check=False,
                           input="y\n", text=True, timeout=300)
    return {"removed": _remove(python.parent.parent)}


def _uninstall_crawl4ai():
    from . import crawl4ai_config
    return {"removed": _remove(crawl4ai_config.environment_dir())}


def _uninstall_search():
    from . import local_search
    return local_search.uninstall_native()


def _uninstall_chromium():
    # Removes the browsers this Playwright installed, unless another install still uses them.
    return {"removed": _run(_playwright("uninstall")) == 0}


def _uninstall_nodriver():
    from .experimental import worker_python
    if not worker_python().is_file():
        return {"removed": None}
    return {"removed": _run([str(worker_python()), "-m", "pip", "uninstall", "--yes", "--quiet", "nodriver"]) == 0}


UNINSTALLERS = {"chromium": _uninstall_chromium, "stealth": _uninstall_stealth, "search": _uninstall_search,
                "crawl4ai": _uninstall_crawl4ai, "nodriver": _uninstall_nodriver}


def uninstall(names):
    """Remove what install put there. Names are required: "all" removes every component."""
    if not names:
        raise ValueError("say what to remove: all, " + ", ".join([*COMPONENTS, *OPTIONAL]))
    chosen = [*resolve(names), *(["nodriver"] if names == ["all"] else [])]
    if "stealth" in chosen and "nodriver" in chosen:
        chosen.remove("nodriver")       # it lives inside the stealth environment
    return {"uninstalled": {name: UNINSTALLERS[name]() for name in chosen}}
