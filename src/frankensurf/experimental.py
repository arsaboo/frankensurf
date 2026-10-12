"""Opt-in public providers isolated from the core Playwright/identity environment."""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import signal

from .runtime import WebFailure, _challenge, _status_failure, _validate_url, parse_content

PROVIDERS = frozenset({"camoufox", "scrapling", "scrapling_http", "patchright", "nodriver"})
_FAILURES = {"PROVIDER_UNAVAILABLE", "PROVIDER_DOWN", "TIMEOUT", "LIMIT_EXCEEDED", "BLOCKED", "RATE_LIMITED", "AUTH_REQUIRED", "NOT_FOUND", "CAPTCHA", "INVALID_URL", "CONTENT_MISMATCH", "POLICY_DENIED"}


def worker_python():
    # Operator configuration selects code, never an agent URL or site response.
    venv = Path.home()/".local/share/frankensurf/provider-venv"
    default = venv/"Scripts"/"python.exe" if os.name == "nt" else venv/"bin"/"python"
    return Path(os.environ.get("FRANKENSURF_PROVIDER_PYTHON", str(default))).expanduser()


# The Camoufox browser build the pinned camoufox package is tested with.
CAMOUFOX_BROWSER = "official/152.0.4-beta.31"


def install_free_providers(log=print):
    """Create the isolated environment for the free stealth providers (Camoufox,
    Scrapling, Patchright) and download their browsers. Safe to run again.

    They live apart from the core because Camoufox pins an older Playwright.
    """
    import venv
    target = worker_python()
    root = target.parent.parent
    if not target.is_file():
        log(f"Creating the stealth provider environment in {root}")
        venv.EnvBuilder(with_pip=True).create(root)
    requirements = Path(__file__).with_name("provider_requirements.txt")
    steps = [("Installing Camoufox, Scrapling and Patchright",
              [str(target), "-m", "pip", "install", "--quiet", "-r", str(requirements)]),
             ("Downloading the Camoufox browser", [str(target), "-m", "camoufox", "fetch", CAMOUFOX_BROWSER]),
             ("Downloading the Patchright browser", [str(target), "-m", "patchright", "install", "chromium"])]
    errors = {}
    for label, command in steps:
        log(label)
        # Each step on its own: a browser that won't download on this platform
        # leaves the others working.
        error = _run_step(command, log, verify=_camoufox_ready if "camoufox" in command else None)
        if error:
            errors[label] = error
            log(f"  failed: {error}")
    result = {provider: installed(provider) for provider in ("camoufox", "scrapling", "patchright")}
    if errors:
        result["errors"] = errors
    return result


def _camoufox_ready():
    from .provider_worker import _camoufox_binaries
    return len(_camoufox_binaries(CAMOUFOX_BROWSER.split("/")[1])) == 1


# `camoufox fetch` reports its own failures on stdout and still exits 0: an
# unreachable or rate-limited GitHub API (60 requests an hour per address
# without a token, shared by whole offices and CI fleets) leaves no browser.
_FETCH_HINT = " (GitHub rate limit? set GITHUB_TOKEN to any token and run setup again)"


def _run_step(command, log, verify=None, attempts=3, pause=10):
    """Run one install command; None when it worked, else a short reason.

    With `verify`, a zero exit status is not trusted: the check must pass too,
    and a failed attempt is retried after a pause."""
    import subprocess
    import time
    error = None
    for attempt in range(1, attempts + 1 if verify else 2):
        done = subprocess.run(command, capture_output=True, text=True)
        output = ((done.stdout or "") + "\n" + (done.stderr or "")).replace("\r", "\n")
        lines = [line.strip() for line in output.splitlines()
                 if line.strip() and "addon" not in line.lower()]
        failed = done.returncode != 0 or (verify is not None and not verify())
        if not failed:
            return None
        error = (" | ".join(lines[-3:])[:400] or f"exit {done.returncode}") + (_FETCH_HINT if verify else "")
        if attempt < attempts and verify:
            log(f"  not installed (attempt {attempt}); trying again in {pause * attempt}s")
            time.sleep(pause * attempt)
    return error


def installed(provider):
    executable = worker_python()
    if not executable.is_absolute() or not executable.is_file(): return False
    package = ("scrapling" if provider.startswith("scrapling")
               else provider if provider in ("patchright", "nodriver") else "camoufox")
    root = executable.parent.parent
    sites = list((root/"lib").glob("python*/site-packages")) + [root/"Lib/site-packages"]
    if not any((site/package).is_dir() for site in sites):
        return False
    # The package alone does not run: Camoufox is the pip package plus a browser
    # build that `camoufox fetch` can fail to download.
    return package != "camoufox" or _camoufox_ready()


def _consume_progress(raw, previous_stage=None):
    from .provider_worker import FAILURE_STAGES
    while raw.startswith(b"@stage ") and b"\n" in raw:
        marker, _, raw = raw.partition(b"\n")
        stage = marker[len(b"@stage "):].decode("ascii", errors="replace")
        if stage in FAILURE_STAGES:
            previous_stage = stage
            from .providers import report_provider_stage
            report_provider_stage(stage)
    return bytearray(raw), previous_stage


class _RuntimeProxy:
    value = None


# Set by read_public for the duration of one worker call.
runtime_proxy = _RuntimeProxy()


async def _packet(url, policy, provider):
    executable = worker_python()
    if not executable.is_absolute() or not executable.is_file():
        raise WebFailure("PROVIDER_UNAVAILABLE", "Optional public provider environment is not installed")
    request = {"report_stages": True, "retain_public_failure_evidence": policy.retain_public_failure_evidence, "provider": provider, "url": url, "timeout_seconds": policy.timeout_seconds,
               "provider_deadline_grace_seconds": policy.provider_deadline_grace_seconds,
               "provider_cleanup_grace_seconds": policy.provider_cleanup_grace_seconds,
               "max_bytes": policy.max_bytes, "settle_ms": policy.settle_ms, "public_browser_headless":policy.public_browser_headless,
               "public_entry_url":policy.public_entry_url,"public_entry_continue_failures":policy.public_entry_continue_failures,"terminal_failures":policy.terminal_failures,
               "scrapling_navigation_wait_until": policy.scrapling_navigation_wait_until, "scrapling_load_dom": policy.scrapling_load_dom, "scrapling_google_search":policy.scrapling_google_search, "scrapling_solve_cloudflare": policy.scrapling_solve_cloudflare, "wait_selector": policy.wait_selector, "wait_state": policy.wait_state, "content_ready_selector": policy.content_ready_selector, "content_ready_timeout_seconds": policy.content_ready_timeout_seconds, "max_pages": policy.max_pages}
    for key in ("capture_json_responses", "capture_json_max_items", "capture_json_max_bytes"):
        request[key]=getattr(policy,key)
    from .runtime import _scroll_screens
    request["scroll_screens"] = _scroll_screens(policy)
    from .profiles import ACTIVE
    profile = ACTIVE.get() if getattr(policy, "profile", None) else None
    if profile is not None:
        # Through the worker's stdin only; the worker never writes it to disk.
        request["profile_context"] = profile.context_options()
    # The owner's proxy, through stdin like profile material; the worker never stores it.
    proxy = getattr(runtime_proxy, "value", None)
    if proxy:
        request["proxy"] = proxy
    # Do not inherit provider keys, proxies, account/profile overrides or Python hooks.
    permitted = {"HOME", "PATH", "LANG", "LC_ALL", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY", "TMPDIR"}
    env = {key: value for key, value in os.environ.items() if key in permitted}
    env.update(PYTHONUNBUFFERED="1", PYTHONNOUSERSITE="1")
    try:
        process = await asyncio.create_subprocess_exec(str(executable), str(Path(__file__).with_name("provider_worker.py")),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            # The worker's errors stay private unless the owner asks to see them.
            stderr=None if os.environ.get("FRANKENSURF_DEBUG_WORKERS") == "1" else asyncio.subprocess.DEVNULL,
            env=env, start_new_session=True)
    except OSError:
        raise WebFailure("PROVIDER_UNAVAILABLE", "Optional public provider interpreter could not be started") from None
    maximum = policy.max_bytes*2 + 12*1024*1024
    last_stage = None
    async def exchange():
        nonlocal last_stage
        process.stdin.write(json.dumps(request).encode())
        await process.stdin.drain()
        process.stdin.close()
        raw = bytearray()
        output_bytes = 0
        while chunk := await process.stdout.read(65536):
            raw.extend(chunk)
            output_bytes += len(chunk)
            raw, last_stage = _consume_progress(raw,last_stage)
            if output_bytes > maximum:
                raise WebFailure("LIMIT_EXCEEDED", "Optional provider output byte limit exceeded")
        await process.wait()
        if process.returncode:
            raise WebFailure("PROVIDER_DOWN", "Optional public provider process failed")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise WebFailure("PROVIDER_DOWN", "Optional provider returned an invalid protocol response") from None
        if not isinstance(result, dict):
            raise WebFailure("PROVIDER_DOWN", "Optional provider returned an invalid protocol response")
        return result
    try:
        return await asyncio.wait_for(exchange(), timeout=policy.timeout_seconds + policy.provider_deadline_grace_seconds)
    except asyncio.TimeoutError:
        raise WebFailure("TIMEOUT", "Optional public provider deadline exceeded" + (" at " + last_stage if last_stage else ""), failure_stage=last_stage) from None
    finally:
        # The browser can outlive a worker that returned or crashed. The group is
        # owned by this invocation even after its leader exits; clean up either way.
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        if process.returncode is None: await process.wait()


def _validated_content_readiness(packet, policy):
    value = packet.get("content_readiness")
    if policy.content_ready_selector is None:
        if value is not None:
            raise WebFailure("PROVIDER_DOWN",
                "Optional provider returned unsolicited content readiness metadata")
        return None
    if (type(value) is not dict
            or set(value) != {"status", "timeout_seconds"}
            or value.get("status") not in {"satisfied", "timed_out"}
            or type(value.get("timeout_seconds")) not in (int, float)
            or value["timeout_seconds"] != policy.content_ready_timeout_seconds):
        raise WebFailure("PROVIDER_DOWN",
            "Optional provider returned invalid content readiness metadata")
    return {"status": value["status"],
            "timeout_seconds": value["timeout_seconds"]}


def _raise_public_failure(runtime, policy, packet, failure):
    # This executor only uses anonymous temporary profiles. Named identities are
    # rejected before launch. Private artifacts are references on failed attempts.
    content = packet.get("content")
    mime = packet.get("content_type")
    if (policy.retain_public_failure_evidence and not policy.identity
            and failure.response_url is not None and not failure.code.startswith("IDENTITY_")
            and isinstance(content, str) and isinstance(mime, str)
            and len(content.encode()) <= policy.max_bytes):
        failure._public_failure_evidence = [runtime._save_bytes(content.encode(), ".json" if "json" in mime else ".html")]
    raise failure


async def read_public(runtime, url, policy, provider):
    if policy.identity:
        raise WebFailure("IDENTITY_PROVIDER_DENIED", "Experimental providers cannot execute named identities")
    if not policy.allow_local_browser and provider != "scrapling_http":
        raise WebFailure("POLICY_DENIED", "Local browser disabled")
    from .egress import CURRENT as EGRESS
    runtime_proxy.value = EGRESS.get() or getattr(runtime, "proxy", None)
    try:
        packet = await _packet(url, policy, provider)
    finally:
        runtime_proxy.value = None
    from .profiles import ACTIVE
    profile = ACTIVE.get() if getattr(policy, "profile", None) else None
    state = packet.pop("storage_state", None)
    if profile is not None and isinstance(state, dict) and profile.merge(state):
        profile.changed = True
    final_url = packet.get("url")
    if final_url is not None: _validate_url(final_url)
    if packet.get("status") != "ok":
        code = packet.get("failure")
        from .provider_worker import FAILURE_STAGES
        stage = packet.get("failure_stage")
        message = "Optional public provider acquisition failed"
        if isinstance(stage, str) and stage in FAILURE_STAGES: message += " at " + stage
        _raise_public_failure(runtime, policy, packet, WebFailure(code if code in _FAILURES else "PROVIDER_DOWN",
                         message, packet.get("http_status"), response_url=final_url, failure_stage=stage))
    final_url = final_url or url
    _validate_url(final_url)
    status = packet.get("http_status")
    if type(status) is not int:
        raise WebFailure("PROVIDER_DOWN", "Optional provider omitted the response status")
    failure = _status_failure(status)
    if failure: _raise_public_failure(runtime, policy, packet, WebFailure(failure, "Optional provider response requires stop", status, response_url=final_url))
    content = packet.get("content")
    content_type = packet.get("content_type")
    if not isinstance(content, str) or not isinstance(content_type, str):
        raise WebFailure("PROVIDER_DOWN", "Optional provider returned an invalid content representation")
    raw = content.encode()
    if len(raw) > policy.max_bytes:
        raise WebFailure("LIMIT_EXCEEDED", "Optional provider response byte limit exceeded", response_url=final_url)
    if "html" in content_type and _challenge(content):
        _raise_public_failure(runtime, policy, packet, WebFailure("CAPTCHA", "Optional provider challenge page observed", status, response_url=final_url))
    if "html" in content_type:
        parsed = parse_content(content,content_type,final_url,None)
        if len(parsed["text"].strip()) < 80 and "<script" in content.lower():
            _raise_public_failure(runtime, policy, packet, WebFailure("VISUAL_REQUIRED", "Optional provider returned an unresolved JavaScript shell", status, response_url=final_url))
    content_readiness = _validated_content_readiness(packet, policy)
    result = {"url": final_url, "content": content, "raw": raw,
              "content_type": content_type, "http_status": status, "headers": {},
              **({"content_readiness": content_readiness}
                 if content_readiness is not None else {})}
    if policy.capture_json_responses and isinstance(packet.get("captured_json"), dict):
        # Validated and bounded again by the provider envelope before export.
        result["captured_json"] = packet["captured_json"]
    encoded = packet.get("screenshot_base64")
    if encoded:
        try: screenshot = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            raise WebFailure("PROVIDER_DOWN", "Optional provider returned an invalid screenshot") from None
        if len(screenshot) > 8*1024*1024:
            raise WebFailure("LIMIT_EXCEEDED", "Optional provider screenshot byte limit exceeded")
        result["screenshot"] = runtime._save_bytes(screenshot, ".png")
    return result
