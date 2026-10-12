import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = (
    Path(__file__).parents[1]
    / "src/frankensurf/crawl4ai_preflight.py")
INSTALLER = (
    Path(__file__).parents[1]
    / "src/frankensurf/install_crawl4ai.sh")
CANARY = (
    Path(__file__).parents[1]
    / "scripts/crawl4ai_isolation_canary.py")


def _preflight_module():
    spec = importlib.util.spec_from_file_location(
        "frankensurf_crawl4ai_preflight_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canary_module():
    spec = importlib.util.spec_from_file_location(
        "frankensurf_crawl4ai_canary_test", CANARY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_preflight_precedes_every_provider_tree_mutation():
    source = INSTALLER.read_text()
    preflight = source.index("crawl4ai_preflight.py")
    assert preflight < source.index('-m venv "$provider_dir"')
    assert preflight < source.index('mkdir -p "$browser_dir"')
    assert preflight < source.index("-m pip install")
    assert preflight < source.index("-m playwright install chromium")
    assert preflight < source.index("-m patchright install chromium")


def test_canary_evidence_binding_requires_clean_exact_source_and_executor():
    module = _canary_module()
    observation = {
        "provider_binding_id": "b" * 64,
        "executor_binding_id": "e" * 64,
    }
    clean = {"commit": "a" * 40, "tree": "c" * 40,
             "status": "", "clean": True}
    qualified = module._evidence_binding(clean, dict(clean), observation)
    assert qualified["eligible"] is True
    assert qualified["source_commit"] == "a" * 40
    assert qualified["source_tree"] == "c" * 40
    assert qualified["source_clean_before"] is True
    assert qualified["source_clean_after"] is True
    assert qualified["source_state_unchanged"] is True
    assert qualified["source_status_before_sha256"] == (
        qualified["source_status_after_sha256"])
    assert qualified["provider_binding_id"] == "b" * 64
    assert qualified["executor_binding_id"] == "e" * 64
    assert "began and ended" in qualified["basis"]

    dirty_start = {**clean, "status": " M src/changed.py", "clean": False}
    dirty = module._evidence_binding(dirty_start, clean, observation)
    assert dirty["eligible"] is False
    assert dirty["source_clean_before"] is False
    assert dirty["source_clean_after"] is True
    assert dirty["source_state_unchanged"] is False
    assert "diagnostic only" in dirty["basis"]

    incomplete = module._evidence_binding(
        clean, dict(clean),
        {**observation, "executor_binding_id": "invalid"})
    assert incomplete["eligible"] is False


def test_canary_brackets_acquisition_with_source_snapshots():
    main = CANARY.read_text().split("def main(argv=None):", 1)[1]
    before = main.index("source_before = _source_snapshot()")
    acquire = main.index("asyncio.run(_acquire")
    after = main.index("source_after = _source_snapshot()")
    binding = main.index("evidence_binding = _evidence_binding")
    assert before < acquire < after < binding


def test_crawl4ai_install_preflight_accepts_owned_private_parent(tmp_path):
    parent = tmp_path / "owned"
    parent.mkdir(mode=0o700)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(parent / "provider")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert not (parent / "provider").exists()


def _provider_with_python314_unicode_alias(tmp_path):
    provider = tmp_path / "provider"
    binaries = provider / "bin"
    binaries.mkdir(parents=True)
    provider.chmod(0o700)
    system_python = Path("/usr/bin/python3.14")
    if not system_python.is_file():
        pytest.skip("requires a protected /usr/bin/python3.14")
    system_python = system_python.resolve()
    if (system_python.parent != Path("/usr/bin")
            or system_python.name != "python3.14"):
        pytest.skip("requires a protected /usr/bin/python3.14")
    (provider / "pyvenv.cfg").write_text("version = 3.14.4\n")
    (binaries / "python3").symlink_to(system_python)
    (binaries / "𝜋thon").symlink_to("python3")
    return provider


def test_python314_venv_metadata_check_is_version_specific(tmp_path):
    module = _preflight_module()
    metadata = tmp_path / "pyvenv.cfg"
    metadata.write_text("version = 3.14.4\n")

    assert module._python314_venv(tmp_path, os.getuid()) is True
    metadata.write_text("version = 3.13.9\n")
    assert module._python314_venv(tmp_path, os.getuid()) is False


def test_crawl4ai_install_preflight_accepts_python314_unicode_alias_twice(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)

    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(provider)],
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
    assert os.readlink(provider / "bin/𝜋thon") == "python3"


def test_crawl4ai_install_preflight_rejects_unicode_alias_outside_python314(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)
    (provider / "pyvenv.cfg").write_text("version = 3.13.9\n")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "only valid in a protected Python 3.14 venv" in result.stderr


def test_crawl4ai_install_preflight_rejects_unicode_alias_wrong_target(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)
    alias = provider / "bin/𝜋thon"
    alias.unlink()
    alias.symlink_to("python")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "exact Python 3.14 venv alias" in result.stderr


def test_crawl4ai_install_preflight_rejects_unicode_alias_wrong_location(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)
    (provider / "bin/𝜋thon").unlink()
    (provider / "𝜋thon").symlink_to("bin/python3")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "unexpected symlink" in result.stderr


def test_crawl4ai_install_preflight_rejects_unprotected_unicode_alias_chain(
        tmp_path):
    provider = tmp_path / "provider"
    binaries = provider / "bin"
    binaries.mkdir(parents=True)
    provider.chmod(0o700)
    local_python = provider / "python3.14"
    local_python.write_text("#!/bin/sh\n")
    local_python.chmod(0o700)
    (binaries / "python3").symlink_to("../python3.14")
    (binaries / "𝜋thon").symlink_to("python3")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "protected system Python" in result.stderr


def test_crawl4ai_install_preflight_rejects_externally_owned_alias_chain(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)
    redirect = tmp_path / "python-redirect"
    redirect.symlink_to(Path(
        getattr(sys, "_base_executable", sys.executable)).resolve())
    python3 = provider / "bin/python3"
    python3.unlink()
    python3.symlink_to(redirect)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "link directly to a venv or system Python" in result.stderr


def test_crawl4ai_install_preflight_rejects_writable_unicode_alias_parent(
        tmp_path):
    provider = _provider_with_python314_unicode_alias(tmp_path)
    (provider / "bin").chmod(0o777)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "owner-controlled and non-writable" in result.stderr


def test_crawl4ai_install_preflight_rejects_symlink_parent_without_mutation(
        tmp_path):
    owned = tmp_path / "owned"
    outside = tmp_path / "outside"
    owned.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    redirect = owned / "redirect"
    redirect.symlink_to(outside, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(redirect / "provider")],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "symlinked" in result.stderr
    assert list(outside.iterdir()) == []


def test_crawl4ai_install_preflight_rejects_symlinked_mutable_child(
        tmp_path):
    provider = tmp_path / "provider"
    outside = tmp_path / "outside"
    provider.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (provider / "browsers").symlink_to(
        outside, target_is_directory=True)

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "unexpected symlink" in result.stderr
    assert list(outside.iterdir()) == []


def test_crawl4ai_install_preflight_rejects_nested_site_package_symlink(
        tmp_path):
    provider = tmp_path / "provider"
    package = provider / "lib/python3.14/site-packages/example"
    outside = tmp_path / "outside"
    package.mkdir(parents=True, mode=0o700)
    provider.chmod(0o700)
    outside.mkdir(mode=0o700)
    (package / "runtime.py").symlink_to(outside / "runtime.py")

    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(provider)],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode != 0
    assert "unexpected symlink" in result.stderr
    assert list(outside.iterdir()) == []
