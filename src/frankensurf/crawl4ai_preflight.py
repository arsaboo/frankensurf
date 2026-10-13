#!/usr/bin/env python3
"""Read-only no-follow preflight for the Crawl4AI provider installer."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys


def _fail(message):
    raise SystemExit("Unsafe Crawl4AI install path: " + message)


def _acceptable_ancestor(details, uid):
    if details.st_uid not in {0, uid}:
        return False
    writable = stat.S_IMODE(details.st_mode) & 0o022
    return not writable or (
        details.st_uid == 0 and details.st_mode & stat.S_ISVTX)


def _python314_venv(root, uid):
    """Recognize bounded, owner-controlled Python 3.14 venv metadata."""
    descriptor = None
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(root / "pyvenv.cfg", flags)
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode)
                or details.st_uid != uid
                or stat.S_IMODE(details.st_mode) & 0o022):
            return False
        raw = os.read(descriptor, 16 * 1024 + 1)
        if len(raw) > 16 * 1024:
            return False
        versions = []
        for line in raw.decode().splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "version":
                versions.append(value.strip())
        return (len(versions) == 1
                and re.fullmatch(r"3\.14(?:\.\d+)?", versions[0])
                    is not None)
    except (OSError, UnicodeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _validate_venv_link(root, path, relative, details, uid):
    if details.st_uid != uid:
        _fail(f"{path} symlink must be owned by the current user")
    try:
        link = os.readlink(path)
    except OSError as error:
        _fail(f"cannot inspect {path}: {error.strerror or 'I/O error'}")
    if relative == Path("lib64"):
        if link != "lib":
            _fail(f"{path} must be the exact venv lib64-to-lib link")
        try:
            target = (root / "lib").lstat()
        except OSError:
            _fail(f"{path} has no validated lib target")
        if (not stat.S_ISDIR(target.st_mode)
                or target.st_uid != uid
                or stat.S_IMODE(target.st_mode) & 0o022):
            _fail(f"{path} has an unsafe lib target")
        return
    python_name = re.compile(r"python(?:\d+(?:\.\d+)?)?")
    python_launcher = (relative.parent == Path("bin")
        and python_name.fullmatch(relative.name) is not None)
    unicode_launcher = relative == Path("bin/𝜋thon")
    if unicode_launcher and link != "python3":
        _fail(f"{path} must be the exact Python 3.14 venv alias to python3")
    if not (python_launcher or unicode_launcher):
        _fail(f"{path} is an unexpected symlink")
    if python_launcher:
        direct = Path(link)
        safe_relative = (not direct.is_absolute()
            and direct.parent == Path(".")
            and python_name.fullmatch(direct.name) is not None)
        safe_system = (direct.is_absolute()
            and direct.parent == Path("/usr/bin")
            and python_name.fullmatch(direct.name) is not None)
        if not (safe_relative or safe_system):
            _fail(
                f"{path} must link directly to a venv or system Python")
    try:
        target = path.resolve(strict=True)
        target_details = target.stat()
    except (OSError, RuntimeError):
        _fail(f"{path} has an invalid interpreter target")
    if (target.parent != Path("/usr/bin")
            or re.fullmatch(r"python\d+(?:\.\d+)?", target.name) is None
            or not stat.S_ISREG(target_details.st_mode)
            or target_details.st_uid != 0
            or stat.S_IMODE(target_details.st_mode) & 0o022):
        _fail(f"{path} must resolve to a protected system Python")
    if (unicode_launcher
            and (target.name != "python3.14"
                 or not _python314_venv(root, uid))):
        _fail(f"{path} is only valid in a protected Python 3.14 venv")


def _validate_existing_tree(root, uid):
    """Inspect every existing entry without following directory symlinks."""
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as error:
            _fail(
                f"cannot inspect {directory}: {error.strerror or 'I/O error'}")
        for entry in entries:
            child = Path(entry.path)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as error:
                _fail(f"cannot inspect {child}: {error.strerror or 'I/O error'}")
            try:
                relative = child.relative_to(root)
            except ValueError:
                _fail(f"{child} escapes the provider environment")
            if stat.S_ISLNK(details.st_mode):
                _validate_venv_link(root, child, relative, details, uid)
                continue
            if (details.st_uid != uid
                    or stat.S_IMODE(details.st_mode) & 0o022):
                _fail(f"{child} must be owner-controlled and non-writable")
            if stat.S_ISDIR(details.st_mode):
                pending.append(child)
            elif not stat.S_ISREG(details.st_mode):
                _fail(f"{child} must be a regular file or directory")


def validate(provider_dir):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        _fail("POSIX no-follow path checks are required")
    uid = os.getuid()
    path = Path(provider_dir)
    normalized = Path(os.path.abspath(path))
    if not path.is_absolute() or path != normalized or path == Path("/"):
        _fail("FRANKENSURF_CRAWL4AI_DIR must be normalized and absolute")

    flags = (os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
             | getattr(os, "O_CLOEXEC", 0))
    descriptor = os.open("/", flags)
    current = Path("/")
    missing = False
    try:
        for index, part in enumerate(path.parts[1:]):
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                missing = True
                break
            except OSError:
                _fail(f"{current / part} is missing, symlinked or not a directory")
            os.close(descriptor)
            descriptor = child
            current /= part
            details = os.fstat(descriptor)
            if not _acceptable_ancestor(details, uid):
                _fail(f"{current} has unsafe ownership or permissions")
            if index == len(path.parts[1:]) - 1:
                if (details.st_uid != uid
                        or stat.S_IMODE(details.st_mode) & 0o077):
                    _fail(f"{current} must be owner-private")
        if (missing
                and stat.S_IMODE(os.fstat(descriptor).st_mode) & 0o022):
            _fail(f"new directories cannot be created beneath {current}")
    finally:
        os.close(descriptor)

    if missing:
        return
    _validate_existing_tree(path, uid)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        raise SystemExit("usage: crawl4ai_preflight.py PROVIDER_DIR")
    validate(arguments[0])


if __name__ == "__main__":
    main()
