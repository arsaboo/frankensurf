"""Pinned local Crawl4AI worker installation and health metadata."""
from __future__ import annotations

from functools import lru_cache
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import stat


SDK_VERSION = "0.9.4"
WORKER_SCHEMA = "frankensurf.crawl4ai-worker/v1"
HEALTH_SCHEMA = "frankensurf.crawl4ai-health/v1"
LOCK_SHA256 = "6e7357abd1f3d6d4e8896a02330678a34ecbbd8b55fd3e525894506cbf8493d5"


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


@lru_cache(maxsize=4096)
def _sha256_file_snapshot(
        path, device, inode, mode, uid, size, modified, changed):
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        expected = (device, inode, mode, uid, size, modified, changed)
        observed = (
            before.st_dev, before.st_ino, before.st_mode, before.st_uid,
            before.st_size, before.st_mtime_ns, before.st_ctime_ns,
        )
        if observed != expected or not stat.S_ISREG(before.st_mode):
            return None
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        observed = (
            after.st_dev, after.st_ino, after.st_mode, after.st_uid,
            after.st_size, after.st_mtime_ns, after.st_ctime_ns,
        )
        return digest.hexdigest() if observed == expected else None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sha256_regular_file(path):
    try:
        path = Path(path)
        details = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(details.st_mode):
        return None
    return _sha256_file_snapshot(
        str(path), details.st_dev, details.st_ino, details.st_mode,
        details.st_uid, details.st_size, details.st_mtime_ns,
        details.st_ctime_ns)


def _lock_path():
    return Path(__file__).resolve().with_name("crawl4ai_requirements.lock.txt")


def _canonical_distribution(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _expected_distributions():
    lock = _lock_path()
    try:
        raw = lock.read_bytes()
        if _sha256_bytes(raw) != LOCK_SHA256:
            return None
        expected = {}
        for line in raw.decode().splitlines():
            match = re.fullmatch(r"([^=<>!~ ]+)==([^ ]+)", line)
            if not match:
                return None
            name = _canonical_distribution(match.group(1))
            if name in expected:
                return None
            expected[name] = match.group(2)
        return expected
    except (OSError, UnicodeError, ValueError):
        return None


def _installed_distributions_identity(root):
    # Fingerprint exact pinned wheel records plus live site-package metadata.
    try:
        root = Path(root)
        sites = sorted({
            *(root / "lib").glob("python*/site-packages"),
            root / "Lib/site-packages",
        })
        sites = [site for site in sites if site.is_dir()]
        if not sites:
            return None
        expected = _expected_distributions()
        if expected is None:
            return None
        rows = []
        actual = {}
        for distribution in metadata.distributions(
                path=[str(site) for site in sites]):
            raw_name = distribution.metadata.get("Name")
            version = distribution.version
            if not raw_name or not version:
                return None
            name = _canonical_distribution(raw_name)
            if name in {"pip", "setuptools"}:
                continue
            if name in actual:
                return None
            record = distribution.read_text("RECORD")
            if record is None:
                return None
            actual[name] = version
            rows.append((name, version, _sha256_bytes(record.encode())))
        if actual != expected:
            return None

        tree = []
        uid = os.getuid()
        for site in sites:
            normalized = Path(os.path.abspath(site))
            if (site != normalized or site.resolve(strict=True) != normalized
                    or not normalized.is_relative_to(root.resolve(strict=True))
                    or not _owned_directory_beneath(root, normalized)):
                return None
            for directory, names, files in os.walk(site, followlinks=False):
                names.sort()
                files.sort()
                base = Path(directory)
                for name in [*names, *files]:
                    item = base / name
                    details = item.lstat()
                    if (stat.S_ISLNK(details.st_mode)
                            or details.st_uid != uid
                            or stat.S_IMODE(details.st_mode) & 0o022
                            or not (stat.S_ISDIR(details.st_mode)
                                    or stat.S_ISREG(details.st_mode))):
                        return None
                    tree.append((
                        str(item.relative_to(root)),
                        "d" if stat.S_ISDIR(details.st_mode) else "f",
                        details.st_size, details.st_mtime_ns,
                        details.st_ctime_ns,
                    ))
        payload = {"distributions": sorted(rows), "tree": tree}
        return _sha256_bytes(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode())
    except (OSError, RuntimeError, TypeError, ValueError, UnicodeError):
        return None


def _installation_identity(root, executable, browsers, browser):
    try:
        root = Path(root)
        executable_target = Path(executable).resolve(strict=True)
        browsers = Path(browsers)
        browser = Path(browser)
        worker = Path(__file__).with_name("crawl4ai_worker.py")
    except (OSError, RuntimeError, ValueError):
        return None
    # Python 3.14 can add bin/𝜋thon -> python3 to a venv.  The installer
    # preflight validates that exact owner-controlled alias, but it is excluded
    # here because FrankenSurf never resolves or executes it: every worker
    # launch uses the absolute bin/python path returned by worker_python().
    values = {
        "worker_sha256": _sha256_regular_file(worker),
        "pyvenv_cfg_sha256": _sha256_regular_file(root / "pyvenv.cfg"),
        "installed_distributions_sha256":
            _installed_distributions_identity(root),
        "python_executable_sha256":
            _sha256_regular_file(executable_target),
        "browser_executable_sha256": _sha256_regular_file(browser),
        "browser_tree_sha256": _owned_tree_identity(browsers),
    }
    return values if all(values.values()) else None


def environment_dir() -> Path:
    configured = os.environ.get("FRANKENSURF_CRAWL4AI_DIR")
    if configured:
        return Path(configured).expanduser()
    python = os.environ.get("FRANKENSURF_CRAWL4AI_PYTHON")
    if python:
        return Path(python).expanduser().parent.parent
    return Path.home() / ".local/share/frankensurf/crawl4ai-venv"


def worker_python() -> Path:
    """Return the sole absolute venv entry that may launch the worker."""
    configured = os.environ.get("FRANKENSURF_CRAWL4AI_PYTHON")
    if configured:
        return Path(configured).expanduser()
    root = environment_dir()
    posix = root / "bin/python"
    return posix if posix.exists() else root / "Scripts/python.exe"


def browsers_path() -> Path:
    configured = os.environ.get("FRANKENSURF_CRAWL4AI_BROWSERS_PATH")
    if configured:
        return Path(configured).expanduser()
    return environment_dir() / "browsers"


def health_path() -> Path:
    configured = os.environ.get("FRANKENSURF_CRAWL4AI_HEALTH")
    if configured:
        return Path(configured).expanduser()
    return environment_dir() / "frankensurf-health.json"


def _owned_directory(path: Path, *, private: bool) -> bool:
    if os.name != "posix" or not hasattr(os, "getuid"):
        return False
    try:
        normalized = Path(os.path.abspath(path))
        details = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    forbidden = 0o077 if private else 0o022
    return (path.is_absolute()
            and path == normalized
            and resolved == normalized
            and stat.S_ISDIR(details.st_mode)
            and details.st_uid == os.getuid()
            and stat.S_IMODE(details.st_mode) & forbidden == 0)


def _read_private_regular_file(path: Path, maximum: int) -> bytes | None:
    if (os.name != "posix" or not hasattr(os, "getuid")
            or not hasattr(os, "O_NOFOLLOW")):
        return None
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) & 0o077):
            return None
        chunks = bytearray()
        while len(chunks) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        return bytes(chunks) if len(chunks) <= maximum else None
    except (OSError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_executable_beneath(root: Path, executable: Path) -> bool:
    """Validate an owned executable through no-symlink directory traversal."""
    if (os.name != "posix" or not hasattr(os, "getuid")
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")):
        return False
    try:
        relative = executable.relative_to(root)
    except ValueError:
        return False
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        return False
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    descriptor = None
    try:
        descriptor = os.open(root, flags)
        root_details = os.fstat(descriptor)
        if (not stat.S_ISDIR(root_details.st_mode)
                or root_details.st_uid != os.getuid()
                or stat.S_IMODE(root_details.st_mode) & 0o077):
            return False
        for part in relative.parts[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            details = os.fstat(descriptor)
            if (not stat.S_ISDIR(details.st_mode)
                    or details.st_uid != os.getuid()
                    or stat.S_IMODE(details.st_mode) & 0o022):
                return False
        file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_descriptor = os.open(
            relative.parts[-1], file_flags, dir_fd=descriptor)
        try:
            details = os.fstat(file_descriptor)
            return (stat.S_ISREG(details.st_mode)
                    and details.st_uid == os.getuid()
                    and stat.S_IMODE(details.st_mode) & 0o022 == 0
                    and stat.S_IMODE(details.st_mode) & stat.S_IXUSR != 0)
        finally:
            os.close(file_descriptor)
    except (OSError, ValueError):
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_directory_beneath(root: Path, directory: Path) -> bool:
    """Validate every directory component with no-follow traversal."""
    if (os.name != "posix" or not hasattr(os, "getuid")
            or not hasattr(os, "O_DIRECTORY")
            or not hasattr(os, "O_NOFOLLOW")):
        return False
    try:
        root = Path(root)
        directory = Path(directory)
        relative = directory.relative_to(root)
    except (TypeError, ValueError):
        return False
    if any(part in {"", ".", ".."} for part in relative.parts):
        return False
    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    descriptor = None
    try:
        descriptor = os.open(root, flags)
        details = os.fstat(descriptor)
        if (not stat.S_ISDIR(details.st_mode)
                or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) & 0o077):
            return False
        for part in relative.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            details = os.fstat(descriptor)
            if (not stat.S_ISDIR(details.st_mode)
                    or details.st_uid != os.getuid()
                    or stat.S_IMODE(details.st_mode) & 0o022):
                return False
        return True
    except (OSError, ValueError):
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _owned_tree_snapshot(root: Path, *, hash_contents: bool):
    """Inventory an owner-controlled tree without accepting symlink entries."""
    if os.name != "posix" or not hasattr(os, "getuid"):
        return None
    try:
        root = Path(root)
        normalized = Path(os.path.abspath(root))
        if (root != normalized or not _owned_directory(root, private=True)
                or not _owned_directory_beneath(root, root)):
            return None
        uid = os.getuid()
        rows = []
        paths = []

        def add(path, kind, details):
            digest = None
            if kind == "f" and hash_contents:
                digest = _sha256_regular_file(path)
                if digest is None:
                    raise OSError("tree file changed during hashing")
            rows.append((
                "." if path == root else str(path.relative_to(root)),
                kind, stat.S_IMODE(details.st_mode), details.st_size,
                details.st_mtime_ns, details.st_ctime_ns, digest,
            ))
            paths.append(path)

        root_details = root.lstat()
        add(root, "d", root_details)

        def fail(error):
            raise error

        for directory, names, files in os.walk(
                root, topdown=True, onerror=fail, followlinks=False):
            names.sort()
            files.sort()
            base = Path(directory)
            if not _owned_directory_beneath(root, base):
                return None
            for name in [*names, *files]:
                item = base / name
                details = item.lstat()
                if (stat.S_ISLNK(details.st_mode)
                        or details.st_uid != uid
                        or stat.S_IMODE(details.st_mode) & 0o022):
                    return None
                if stat.S_ISDIR(details.st_mode):
                    kind = "d"
                elif stat.S_ISREG(details.st_mode):
                    kind = "f"
                else:
                    return None
                add(item, kind, details)
        rows.sort(key=lambda row: row[0])
        paths.sort(key=str)
        return rows, paths
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _owned_tree_identity(root: Path):
    """Bind all metadata and bytes in an owner-controlled executor tree."""
    snapshot = _owned_tree_snapshot(root, hash_contents=True)
    if snapshot is None:
        return None
    rows, _ = snapshot
    return _sha256_bytes(json.dumps(
        rows, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode())


def _owned_python_entry(root: Path, executable: Path) -> bool:
    """Allow a venv Python symlink only from an owner-protected environment."""
    try:
        executable.relative_to(root)
        if not _owned_directory_beneath(root, executable.parent):
            return False
        entry = executable.lstat()
        if entry.st_uid != os.getuid() or not (
                stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode)):
            return False
        target = executable.resolve(strict=True)
        target_details = target.stat()
    except (OSError, RuntimeError, ValueError):
        return False
    return (stat.S_ISREG(target_details.st_mode)
            and target_details.st_uid in {0, os.getuid()}
            and stat.S_IMODE(target_details.st_mode) & 0o022 == 0
            and os.access(executable, os.X_OK))


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate health field")
        value[key] = item
    return value


def _basic_health_payload():
    """Validate the private stamp and executable paths without a tree scan."""
    root = environment_dir()
    executable = worker_python()
    browsers = browsers_path()
    stamp = health_path()
    if (not _owned_directory(root, private=True)
            or not executable.is_absolute()
            or executable != Path(os.path.abspath(executable))
            or not _owned_python_entry(root, executable)
            or not _owned_directory(browsers, private=True)
            or not _owned_directory(stamp.parent, private=True)):
        return None
    raw = _read_private_regular_file(stamp, 32 * 1024)
    if raw is None:
        return None
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError):
        return None
    if type(payload) is not dict:
        return None
    expected = {
        "schema", "status", "sdk_version", "python", "browsers_path",
        "browser_executable", "requirements_sha256", "worker_sha256",
        "pyvenv_cfg_sha256",
        "installed_distributions_sha256", "python_executable_sha256",
        "browser_executable_sha256", "browser_tree_sha256",
    }
    try:
        configured_python = executable.absolute()
        configured_browsers = browsers.absolute()
        browser = Path(payload["browser_executable"])
    except (KeyError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if (set(payload) != expected
            or payload.get("schema") != HEALTH_SCHEMA
            or payload.get("status") != "ok"
            or payload.get("sdk_version") != SDK_VERSION
            or payload.get("requirements_sha256") != LOCK_SHA256
            or payload.get("python") != str(configured_python)
            or payload.get("browsers_path") != str(configured_browsers)
            or not browser.is_absolute()
            or browser != Path(os.path.abspath(browser))
            or not _owned_executable_beneath(configured_browsers, browser)
            or any(type(payload.get(name)) is not str
                   or re.fullmatch(r"[0-9a-f]{64}", payload[name]) is None
                   for name in (
                       "worker_sha256", "pyvenv_cfg_sha256",
                       "installed_distributions_sha256",
                       "python_executable_sha256",
                       "browser_executable_sha256",
                       "browser_tree_sha256"))):
        return None
    return payload


def _validated_health_payload():
    payload = _basic_health_payload()
    if payload is None:
        return None
    root = environment_dir()
    executable = worker_python().absolute()
    browsers = browsers_path().absolute()
    browser = Path(payload["browser_executable"])
    identity = _installation_identity(root, executable, browsers, browser)
    if (identity is None
            or any(payload.get(name) != value
                   for name, value in identity.items())):
        return None
    return payload


def _path_guard(path):
    """Return a no-follow metadata token for a startup-bound path."""
    try:
        path = Path(path)
        details = path.lstat()
        target = os.readlink(path) if stat.S_ISLNK(details.st_mode) else None
        return (
            str(path), details.st_dev, details.st_ino, details.st_mode,
            details.st_uid, details.st_gid, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns, target,
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _runtime_guard(payload):
    """Build a cheap drift guard around the immutable startup fingerprint."""
    try:
        root = environment_dir()
        executable = worker_python().absolute()
        executable_target = executable.resolve(strict=True)
        browsers = browsers_path().absolute()
        browser = Path(payload["browser_executable"])
        sites = sorted({
            *(root / "lib").glob("python*/site-packages"),
            root / "Lib/site-packages",
        })
        sites = [site for site in sites if site.is_dir()]
        if not sites:
            return None
        paths = {
            root, root / "pyvenv.cfg", executable, executable_target,
            browsers, browser, health_path(), _lock_path(),
            Path(__file__).with_name("crawl4ai_worker.py"),
        }
        browser_snapshot = _owned_tree_snapshot(
            browsers, hash_contents=False)
        if browser_snapshot is None:
            return None
        _, browser_paths = browser_snapshot
        paths.update(browser_paths)
        for site in sites:
            current = site
            while current != root:
                paths.add(current)
                current = current.parent
            paths.update(site.glob("*.dist-info/METADATA"))
            paths.update(site.glob("*.dist-info/RECORD"))
        guard = tuple(
            token for token in (
                _path_guard(path) for path in sorted(paths, key=str))
            if token is not None)
        if len(guard) != len(paths):
            return None
        return guard
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def runtime_binding_snapshot():
    """Return the full startup binding and its cheap per-read drift guard."""
    payload = _validated_health_payload()
    if payload is None:
        return None, None
    bound = {
        key: payload[key]
        for key in (
            "schema", "sdk_version", "python", "browsers_path",
            "browser_executable", "requirements_sha256", "worker_sha256",
            "pyvenv_cfg_sha256",
            "installed_distributions_sha256", "python_executable_sha256",
            "browser_executable_sha256", "browser_tree_sha256")
    }
    guard = _runtime_guard(payload)
    if guard is None:
        return None, None
    binding = _sha256_bytes(json.dumps(
        bound, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode())
    return binding, guard


def runtime_binding_id():
    """Bind the immutable plugin to the exact healthy local executor."""
    return runtime_binding_snapshot()[0]


def runtime_guard_matches(expected):
    """Check cheap no-follow drift tokens for an already-bound Runtime."""
    if type(expected) is not tuple:
        return False
    payload = _basic_health_payload()
    return payload is not None and _runtime_guard(payload) == expected


def health() -> dict:
    """Return a bounded, secret-free summary after live drift validation."""
    binding, _ = runtime_binding_snapshot()
    if binding is None:
        return {
            "available": False,
            "code": "PROVIDER_UNAVAILABLE",
            "sdk_version": SDK_VERSION,
        }
    return {
        "available": True,
        "code": "ok",
        "sdk_version": SDK_VERSION,
        "runtime_binding_id": binding,
    }


def installed() -> bool:
    return runtime_binding_id() is not None
