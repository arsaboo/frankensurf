import pytest

from frankensurf.runtime import Runtime


@pytest.fixture(autouse=True)
def _no_hosted_network(monkeypatch, tmp_path):
    """Hosted plugins never reach the network or the owner's keys in tests."""
    import httpx
    from frankensurf import hosted_providers
    monkeypatch.setattr(hosted_providers, "TRANSPORT", httpx.MockTransport(
        lambda request: httpx.Response(503, text="hosted network disabled in tests")))
    monkeypatch.setattr(hosted_providers, "_SERVICES", tmp_path / "no-services.json")
    monkeypatch.setattr(hosted_providers, "_ENV_FILE", tmp_path / "no-env-file")
    for name in list(__import__("os").environ):
        if name.startswith("FRANKENSURF_") and name.endswith(("_API_KEY", "_ZONE")):
            monkeypatch.delenv(name)
        # Services' own key names are read too (hosted_providers._setting).
        elif name.endswith(("_API_KEY", "_API_TOKEN")) and not name.startswith("OPENROUTER"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_owner_plugin_policy(monkeypatch, tmp_path):
    """The owner's own trust file (a disabled plugin, say) never changes a test."""
    monkeypatch.setenv("FRANKENSURF_PLUGINS", str(tmp_path / "no-plugin-policy" / "plugins.json"))


@pytest.fixture(autouse=True)
def _no_origin_pacing(monkeypatch):
    """Fake transports need no politeness; tests/test_origin_pacing.py enables it."""
    monkeypatch.setattr(Runtime, "pacing_enabled", False)
    monkeypatch.setattr(Runtime, "completeness_enabled", False)
