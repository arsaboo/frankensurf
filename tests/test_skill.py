"""The agent skill ships with the package, installs, and is the MCP server's instructions."""
import re

import pytest

from frankensurf import skill


def test_skill_has_front_matter_and_names_real_tools():
    text = skill.text()
    assert text.startswith("---\nname: frankensurf\ndescription: ")
    body = skill.body()
    assert not body.startswith("---")
    # Every tool the skill tells agents to call exists on the MCP server.
    pytest.importorskip("mcp")
    from frankensurf import mcp_server
    named = set(re.findall(r"`([a-z_]+)\(", body))
    for tool in named:
        assert hasattr(mcp_server, tool), tool
    assert mcp_server.server.instructions == body


def test_install_copies_into_a_skills_folder(tmp_path):
    path = skill.install(tmp_path / "skills")
    assert path == tmp_path / "skills" / "frankensurf" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == skill.text()


def test_cli_skill_install(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from frankensurf import cli
    monkeypatch.setattr(sys, "argv", ["frankensurf", "skill", "install", "--skill-dir", str(tmp_path)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["installed"].endswith("frankensurf/SKILL.md")


# frankensurf setup writes the skill into an agent's config only when the user says so.

def _setup_env(monkeypatch, tmp_path, *, tty):
    """A fake HOME with ~/.claude in it, no downloads, and the terminal (or not) of the test's choosing."""
    import subprocess
    import sys
    from frankensurf import experimental
    home = tmp_path / "stand-in-home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(subprocess, "run", lambda command, **_: subprocess.CompletedProcess(command, 0))
    monkeypatch.setattr(experimental, "install_free_providers", lambda log: {})
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, "isatty", lambda: tty, raising=False)
    return home / ".claude" / "skills" / "frankensurf" / "SKILL.md"


def _setup(*flags):
    from frankensurf import cli
    return cli._setup(cli.parse_args(["setup", "--no-stealth", *flags]))


def test_setup_without_a_terminal_does_not_write(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt without a terminal"))
    report = _setup()["skill"]
    assert not target.parent.exists()
    assert report["status"].startswith("available, not installed")
    assert report["path"] == str(target) and "--install-skill" in report["install_with"]
    assert report["uninstall_with"] == "frankensurf skill uninstall"


def test_setup_in_ci_does_not_prompt_or_write(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=True)
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt in CI"))
    assert _setup()["skill"]["status"].startswith("available")
    assert not target.parent.exists()


def test_setup_install_skill_flag_writes(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=False)
    report = _setup("--install-skill")["skill"]
    assert target.read_text(encoding="utf-8") == skill.text()
    assert report["status"] == "installed" and report["path"] == str(target)
    assert report["installed_version"] == skill.bundled_version()


def test_setup_interactive_yes_writes_and_names_the_path(monkeypatch, tmp_path, capsys):
    target = _setup_env(monkeypatch, tmp_path, tty=True)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    report = _setup()["skill"]
    assert target.is_file() and report["status"] == "installed"
    err = capsys.readouterr().err
    assert str(target) in err and "[y/N]" in err


def test_setup_interactive_default_does_not_write(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=True)
    monkeypatch.setattr("builtins.input", lambda *a: "")
    report = _setup()["skill"]
    assert not target.parent.exists() and report["status"] == "declined"


def test_setup_no_install_skill_does_not_prompt_or_write(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("must not prompt"))
    report = _setup("--no-install-skill")["skill"]
    assert not target.parent.exists() and "--no-install-skill" in report["status"]


def test_setup_flags_contradict_and_belong_to_setup():
    from frankensurf.cli import parse_args
    with pytest.raises(SystemExit):
        parse_args(["setup", "--install-skill", "--no-install-skill"])
    with pytest.raises(SystemExit):
        parse_args(["read", "https://example.com", "--install-skill"])


def test_setup_asks_before_overwriting_an_existing_skill(monkeypatch, tmp_path):
    target = _setup_env(monkeypatch, tmp_path, tty=True)
    target.parent.mkdir(parents=True)
    target.write_text("an older skill", encoding="utf-8")
    (target.parent / skill.VERSION_FILE).write_text("0.1.0\n", encoding="utf-8")
    # Declined: left alone, with the version difference reported.
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    report = _setup()["skill"]
    assert target.read_text(encoding="utf-8") == "an older skill" and report["status"] == "declined"
    assert report["installed_version"] == "0.1.0" and report["bundled_version"] == skill.bundled_version()
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("--no-install-skill must not prompt"))
    assert _setup("--no-install-skill")["skill"]["status"].startswith("left as is")
    assert target.read_text(encoding="utf-8") == "an older skill"
    # Agreed: overwritten.
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    assert _setup()["skill"]["status"] == "updated"
    assert target.read_text(encoding="utf-8") == skill.text()
    # Now current: nothing to ask.
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("up to date: nothing to ask"))
    assert _setup()["skill"]["status"] == "up to date"


def test_skill_uninstall(tmp_path, monkeypatch, capsys):
    import json
    import sys
    from frankensurf import cli
    skill.install(tmp_path)
    monkeypatch.setattr(sys, "argv", ["frankensurf", "skill", "uninstall", "--skill-dir", str(tmp_path)])
    cli.main()
    out = json.loads(capsys.readouterr().out)
    assert out["removed"].endswith("frankensurf/SKILL.md")
    assert not (tmp_path / "frankensurf").exists()
    cli.main()
    assert json.loads(capsys.readouterr().out)["removed"] is None


def test_the_test_suite_never_sees_the_real_home(tmp_path):
    assert skill.skills_folder() == tmp_path / "stand-in-home" / ".claude" / "skills"
