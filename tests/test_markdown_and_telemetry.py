"""Markdown output, per-attempt failure messages and OpenTelemetry spans (synthetic fixtures)."""
import asyncio

import pytest

from frankensurf import providers, telemetry
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import Runtime, WebFailure, attempt_summary
from frankensurf.to_markdown import html_to_markdown

POLICY = {"origin_route_hint_ttl_seconds": 0, "second_opinion_text_chars": 0, "origin_min_interval_seconds": 0}
PAGE = ("<html><head><title>Guide</title><script>var x = 1;</script></head><body>"
        "<h1>Camera guide</h1><p>Pick a <strong>light</strong> body and a <a href='/lenses'>good lens</a>.</p>"
        + "<p>More useful text about cameras and lenses for everyone.</p>" * 20 +
        "<ul><li>Body<ul><li>Mirrorless</li></ul></li><li>Lens</li></ul>"
        "<table><tr><th>Model</th><th>Price</th></tr><tr><td>A1</td><td>$900</td></tr></table>"
        "<pre><code>print('hi')</code></pre><img src='/a.jpg' alt='A camera'>"
        "<a href='javascript:void(0)'>menu</a><form><input name='q'></form></body></html>")


def test_html_becomes_markdown():
    md = html_to_markdown(PAGE, "https://shop.example/guide")
    assert md.startswith("# Camera guide\n\nPick a **light** body and a [good lens](https://shop.example/lenses).")
    assert "- Body\n  - Mirrorless\n- Lens" in md
    assert "| Model | Price |\n| --- | --- |\n| A1 | $900 |" in md
    assert "```\nprint('hi')\n```" in md
    assert "![A camera](https://shop.example/a.jpg)" in md
    assert "var x" not in md and "javascript:" not in md and "menu" in md


@pytest.fixture
def install(monkeypatch):
    def apply(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return apply


def plugin(identifier, failure=None):
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            if failure:
                raise WebFailure(failure, f"{identifier} met a challenge page")
            return {"url": request.url, "content": PAGE, "content_type": "text/html", "http_status": 200}
    return Plugin()


async def test_markdown_output_on_a_read(tmp_path, install):
    install(plugin("fixture"))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/guide", provider="fixture",
                                policy_overrides={**POLICY, "markdown_output": True})
        plain = await web.read("https://shop.example/guide", provider="fixture", policy_overrides=POLICY)
    assert result["markdown"].startswith("# Camera guide")
    assert "markdown" not in plain


async def test_each_failed_attempt_says_why(tmp_path, install):
    install(plugin("walled", "CAPTCHA"), plugin("works"))
    async with Runtime(tmp_path) as web:
        result = await web.read("https://shop.example/guide", policy_overrides=POLICY)
    summary = attempt_summary(result["receipt"])
    assert summary[0]["provider"] == "walled" and summary[0]["failure"] == "CAPTCHA"
    assert summary[0]["message"] == "walled met a challenge page"
    assert summary[-1]["provider"] == "works" and summary[-1]["status"] == "observed"
    assert set(summary[0]) <= {"provider", "status", "failure", "message", "failure_stage", "latency_ms"}


def test_internal_failure_shorthand_reads_as_plain_words():
    from frankensurf.runtime import _readable_failure
    assert _readable_failure(WebFailure("BLOCKED", "HTTP response requires stop", 403)) == \
        "The site refused the request (HTTP 403)"
    assert _readable_failure(WebFailure("CAPTCHA", "Optional public provider acquisition failed at navigation")) == \
        "The site served a challenge page"
    assert _readable_failure(WebFailure("TIMEOUT", "Browser navigation timed out")) == "Browser navigation timed out"


class FakeSpan:
    def __init__(self, name, start_time=None):
        self.name, self.attributes, self.status, self.ended = name, {}, None, False

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status

    def end(self, end_time=None):
        self.ended = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.ended = True
        return False


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_as_current_span(self, name, **kwargs):
        span = FakeSpan(name)
        self.spans.append(span)
        return span

    def start_span(self, name, start_time=None):
        span = FakeSpan(name, start_time)
        self.spans.append(span)
        return span


async def test_a_read_is_a_span_with_one_child_per_tool(tmp_path, install, monkeypatch):
    tracer = FakeTracer()
    monkeypatch.setattr(telemetry, "_tracer", lambda: tracer)
    monkeypatch.setattr(telemetry, "Status", lambda code, message=None: (code, message), raising=False)
    monkeypatch.setattr(telemetry, "StatusCode", type("SC", (), {"ERROR": "ERROR"}), raising=False)
    install(plugin("walled", "CAPTCHA"), plugin("works"))
    async with Runtime(tmp_path) as web:
        await web.read("https://shop.example/guide?token=secret", policy_overrides=POLICY)
    read, *attempts = tracer.spans
    assert read.name == "frankensurf.read" and read.ended
    assert read.attributes["server.address"] == "shop.example" and read.attributes["url.path"] == "/guide"
    assert not any("secret" in str(value) for value in read.attributes.values())
    assert read.attributes["frankensurf.status"] == "observed" and read.attributes["frankensurf.attempts"] == 2
    assert [span.attributes["frankensurf.provider"] for span in attempts] == ["walled", "works"]
    assert attempts[0].attributes["frankensurf.failure.code"] == "CAPTCHA" and attempts[0].status[0] == "ERROR"


async def test_signed_in_reads_record_only_the_host():
    tracer = FakeTracer()
    original = telemetry._tracer
    telemetry._tracer = lambda: tracer
    try:
        async def work():
            return {"receipt": {"status": "observed", "attempts": []}}
        await telemetry.traced("read", "https://mail.example/inbox/123", work(), private=True)
    finally:
        telemetry._tracer = original
    assert tracer.spans[0].attributes == {"server.address": "mail.example", "frankensurf.status": "observed",
                                          "frankensurf.attempts": 0}


def test_without_opentelemetry_reads_are_untouched(monkeypatch):
    monkeypatch.setattr(telemetry, "_tracer", lambda: None)

    async def work():
        return {"receipt": {"status": "observed"}}
    assert asyncio.run(telemetry.traced("read", "https://a.example/", work())) == {"receipt": {"status": "observed"}}
