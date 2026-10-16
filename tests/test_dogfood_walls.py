"""Pages that came back "observed" but weren't the page (dogfood, 16 October 2026), with
synthetic fixtures: empty bodies, a slider-challenge redirect, a short sign-in prompt in
Chinese, a redirect to the home page; plus links output and the missing-browser message."""
import subprocess
import sys

import pytest

from frankensurf import providers
from frankensurf.providers import ProviderManifest, ProviderRegistry
from frankensurf.runtime import _BROWSER_MISSING, Runtime

POLICY = {"origin_route_hint_ttl_seconds": 0, "second_opinion_text_chars": 0}
ARTICLE = "<html><title>Results</title><body>" + "<p>Result text about cameras.</p>" * 40 + "</body></html>"


def plugin(identifier, content, final_url=None, status=200):
    class Plugin:
        def __init__(self):
            self.manifest = ProviderManifest(identifier, "1")

        async def acquire(self, request, services):
            return {"url": final_url or request.url, "content": content,
                    "content_type": "text/html", "http_status": status}
    return Plugin()


@pytest.fixture
def install(monkeypatch):
    def apply(*items):
        registry = ProviderRegistry()
        for item in items:
            registry.register(item)
        monkeypatch.setattr(providers, "DEFAULT_PROVIDERS", registry)
    return apply


async def read(tmp_path, url, **overrides):
    async with Runtime(tmp_path) as web:
        return await web.read(url, provider="fixture", policy_overrides={**POLICY, **overrides})


async def test_an_empty_page_is_not_a_success_even_when_the_tool_is_named(tmp_path, install):
    install(plugin("fixture", "<html><head></head><body></body></html>", status=202))
    result = await read(tmp_path, "https://search.example/?s=camera")
    assert result["receipt"]["status"] == "failed"
    assert result["receipt"]["failure"]["code"] == "EMPTY_PAGE"


async def test_a_redirect_to_an_empty_punish_page_is_a_challenge(tmp_path, install):
    install(plugin("fixture", "<html><body></body></html>",
                   final_url="https://shop.example.cn/_____tmd_____/punish?x5secdata=abc"))
    result = await read(tmp_path, "https://shop.example.cn/search?keywords=camera")
    assert result["receipt"]["failure"]["code"] == "CAPTCHA"


async def test_a_short_chinese_sign_in_prompt_is_an_auth_wall(tmp_path, install):
    page = "<html><title>搜索</title><body><nav>首页 购物车 我的</nav><p>亲，请登录</p></body></html>"
    install(plugin("fixture", page))
    result = await read(tmp_path, "https://s.shop.example/search?q=camera")
    assert result["receipt"]["failure"]["code"] == "AUTH_REQUIRED"


def test_a_header_only_reader_page_with_a_sign_in_link_is_an_auth_wall():
    from frankensurf.runtime import _wall_after_parse
    long = "https://shop.example/member/login?redirect=" + "x" * 300
    header = "\n".join(f"*   [Link {n}]({long}{n})" for n in range(10))
    markdown = header + f"\n*   [亲，请登录]({long})[免费注册]({long})\n"
    assert len(markdown) > 2500
    assert _wall_after_parse(None, markdown, "https://s.shop.example/search?q=x",
                             "https://s.shop.example/search?q=x") == "AUTH_REQUIRED"


async def test_a_search_that_lands_on_the_home_page_is_not_the_page(tmp_path, install):
    install(plugin("fixture", ARTICLE, final_url="https://www.engine.example/?q=camera&mkt=zh-CN"))
    result = await read(tmp_path, "https://cn.engine.example/search?q=camera")
    assert result["receipt"]["failure"]["code"] == "CONTENT_MISMATCH"


async def test_a_shortener_landing_on_a_home_page_is_fine(tmp_path, install):
    install(plugin("fixture", ARTICLE, final_url="https://brand.example/"))
    result = await read(tmp_path, "https://sho.rt/abc123")
    assert result["receipt"]["status"] == "observed"


async def test_asking_for_index_html_and_getting_the_home_page_is_fine(tmp_path, install):
    install(plugin("fixture", ARTICLE, final_url="https://brand.example/"))
    result = await read(tmp_path, "https://brand.example/index.html")
    assert result["receipt"]["status"] == "observed"


async def test_links_come_back_absolute_in_order_once_each(tmp_path, install):
    page = ("<html><body>" + "<p>Listing text for cameras and lenses.</p>" * 20 +
            '<a href="/item/1#reviews">Camera one</a> <a href="https://shop.example/item/2">Camera two</a>'
            '<a href="/item/1">Camera one again</a> <a href="mailto:x@example.com">mail</a>'
            '<a href="/item/3"><img src="/i.jpg" alt="Camera three"></a></body></html>')
    install(plugin("fixture", page))
    result = await read(tmp_path, "https://shop.example/search?q=camera", include_links=True)
    assert result["links"] == [
        {"url": "https://shop.example/item/1", "text": "Camera one"},
        {"url": "https://shop.example/item/2", "text": "Camera two"},
        {"url": "https://shop.example/item/3", "text": "Camera three"}]
    capped = await read(tmp_path / "b", "https://shop.example/search?q=camera", include_links=True, max_links=1)
    assert len(capped["links"]) == 1
    plain = await read(tmp_path / "c", "https://shop.example/search?q=camera")
    assert "links" not in plain


def test_playwrights_missing_browser_message_is_recognised():
    message = ("BrowserType.launch: Executable doesn't exist at /home/u/.cache/ms-playwright/chromium-1248/"
               "chrome-linux/chrome\nLooks like Playwright was just installed or updated.\n"
               "Please run the following command to download new browsers: playwright install")
    assert _BROWSER_MISSING.search(message)
    assert not _BROWSER_MISSING.search("Target page, context or browser has been closed")


def test_python_dash_m_frankensurf_runs_the_cli():
    done = subprocess.run([sys.executable, "-m", "frankensurf", "--help"], capture_output=True, text=True)
    assert done.returncode == 0 and "operation" in done.stdout
