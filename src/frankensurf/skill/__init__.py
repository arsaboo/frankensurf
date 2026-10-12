"""The FrankenSurf agent skill: how an agent should use the tools (SKILL.md)."""
from __future__ import annotations

from pathlib import Path

SKILL = Path(__file__).with_name("SKILL.md")


def text() -> str:
    return SKILL.read_text(encoding="utf-8")


def body() -> str:
    """The guidance without its YAML front matter (for MCP server instructions)."""
    content = text()
    if content.startswith("---"):
        content = content.split("---", 2)[2]
    return content.strip()


VERSION_FILE = ".frankensurf-version"


def bundled_version() -> str:
    from importlib import metadata
    try:
        return metadata.version("frankensurf")
    except metadata.PackageNotFoundError:
        return "unknown"


def default_folder() -> Path:
    return Path.home() / ".claude" / "skills"


def skills_folder(directory: str | Path | None = None) -> Path:
    """The skills folder: DIRECTORY, or ~/.claude/skills."""
    return Path(directory).expanduser() if directory else default_folder()


def path_for(directory: str | Path | None = None) -> Path:
    """Where the skill is (or would be) written."""
    return skills_folder(directory) / "frankensurf" / "SKILL.md"


def installed(directory: str | Path | None = None) -> dict | None:
    """What is installed there now: its path, the FrankenSurf version that wrote it
    (None if unknown) and whether it matches the bundled skill. None if not installed."""
    path = path_for(directory)
    if not path.is_file():
        return None
    try:
        version = (path.parent / VERSION_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        version = None
    try:
        current = path.read_text(encoding="utf-8") == text()
    except (OSError, UnicodeDecodeError):
        current = False
    return {"path": str(path), "version": version, "current": current}


def install(directory: str | Path | None = None) -> Path:
    """Copy SKILL.md into a skills folder (default ~/.claude/skills/frankensurf).
    This writes into an agent's configuration: callers ask the user first."""
    path = path_for(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text(), encoding="utf-8")
    (path.parent / VERSION_FILE).write_text(bundled_version() + "\n", encoding="utf-8")
    return path


def uninstall(directory: str | Path | None = None) -> Path | None:
    """Remove what install wrote (SKILL.md and its version note); the folder too if
    nothing else is in it. Returns the removed SKILL.md path, or None if not installed."""
    path = path_for(directory)
    if not path.is_file():
        return None
    path.unlink()
    (path.parent / VERSION_FILE).unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass  # something else lives there: leave it
    return path
