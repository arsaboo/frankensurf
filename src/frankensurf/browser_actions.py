"""Caller-named browser actions before a page is captured: Firecrawl's scrape `actions`.

    {"type": "wait", "milliseconds": 1500}          {"type": "wait", "selector": "#results"}
    {"type": "click", "selector": "button.more"}    {"type": "click", "selector": ".tab", "all": true}
    {"type": "write", "text": "lenses"}             (types into whatever has focus: click a field first)
    {"type": "press", "key": "Enter"}
    {"type": "scroll", "direction": "down"}         {"type": "scroll", "direction": "up", "selector": "#list"}
    {"type": "screenshot", "fullPage": true}
    {"type": "scrape"}                              (the page's HTML at this point)
    {"type": "executeJavascript", "script": "return document.title"}

Unlike interactions.py, which only ever presses what web conventions say is safe, these do exactly
what the caller names: they are the caller's own instructions. So they run only in Frankensurf's own
isolated browser on public reads, never with a named identity or a stored profile, where a click
could act as you. Every action is reported; a failed action stops the rest and says which.
"""
from __future__ import annotations

TYPES = ("wait", "click", "write", "press", "scroll", "screenshot", "scrape", "executeJavascript")
MAX_ACTIONS = 50
MAX_WAIT_MS = 30_000


def validate(actions) -> tuple[dict, ...]:
    """The actions as a tuple of plain dicts; ValueError on anything malformed."""
    if not isinstance(actions, (list, tuple)) or len(actions) > MAX_ACTIONS:
        raise ValueError(f"actions must be a list of at most {MAX_ACTIONS}")
    out = []
    for action in actions:
        if not isinstance(action, dict) or action.get("type") not in TYPES:
            raise ValueError("each action needs a type: " + ", ".join(TYPES))
        kind = action["type"]

        def text(field, limit, required=False):
            value = action.get(field)
            if value is None and not required:
                return None
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValueError(f"{kind}.{field} must be a string of 1 to {limit} characters")
            return value
        clean = {"type": kind}
        if kind == "wait":
            if action.get("selector") is not None:
                clean["selector"] = text("selector", 500)
            else:
                ms = action.get("milliseconds")
                if not isinstance(ms, int) or isinstance(ms, bool) or not 0 <= ms <= MAX_WAIT_MS:
                    raise ValueError(f"wait.milliseconds must be 0 to {MAX_WAIT_MS}, or give a selector")
                clean["milliseconds"] = ms
        elif kind == "click":
            clean["selector"] = text("selector", 500, required=True)
            clean["all"] = bool(action.get("all"))
        elif kind == "write":
            clean["text"] = text("text", 5000, required=True)
        elif kind == "press":
            clean["key"] = text("key", 50, required=True)
        elif kind == "scroll":
            direction = action.get("direction", "down")
            if direction not in ("up", "down"):
                raise ValueError("scroll.direction is up or down")
            clean["direction"] = direction
            if action.get("selector") is not None:
                clean["selector"] = text("selector", 500)
        elif kind == "screenshot":
            clean["fullPage"] = bool(action.get("fullPage"))
        elif kind == "executeJavascript":
            clean["script"] = text("script", 20_000, required=True)
        out.append(clean)
    return tuple(out)


async def run(page, actions, save_bytes) -> dict:
    """Perform the actions in order on a Playwright page. save_bytes(bytes, ext) stores a screenshot
    and returns its evidence record."""
    from .runtime import _rendered_html
    done = {"performed": [], "screenshots": [], "scrapes": [], "javascriptReturns": []}
    for index, action in enumerate(actions):
        kind = action["type"]
        try:
            if kind == "wait":
                if "selector" in action:
                    await page.locator(action["selector"]).first.wait_for(state="visible", timeout=MAX_WAIT_MS)
                else:
                    await page.wait_for_timeout(action["milliseconds"])
            elif kind == "click":
                targets = page.locator(action["selector"])
                if action["all"]:
                    for position in range(min(await targets.count(), 100)):
                        await targets.nth(position).click(timeout=10_000)
                else:
                    await targets.first.click(timeout=10_000)
                await page.wait_for_timeout(300)
            elif kind == "write":
                await page.keyboard.type(action["text"], delay=20)
            elif kind == "press":
                await page.keyboard.press(action["key"])
                await page.wait_for_timeout(300)
            elif kind == "scroll":
                step = "window.innerHeight" if action["direction"] == "down" else "-window.innerHeight"
                if "selector" in action:
                    await page.locator(action["selector"]).first.evaluate(
                        f"(e) => e.scrollBy(0, {'e.clientHeight' if action['direction'] == 'down' else '-e.clientHeight'})")
                else:
                    await page.evaluate(f"() => window.scrollBy(0, {step})")
                await page.wait_for_timeout(400)
            elif kind == "screenshot":
                done["screenshots"].append(save_bytes(await page.screenshot(full_page=action["fullPage"]), ".png"))
            elif kind == "scrape":
                done["scrapes"].append({"url": page.url, "html": await _rendered_html(page)})
            elif kind == "executeJavascript":
                value = await page.evaluate(f"async () => {{ {action['script']} }}")
                done["javascriptReturns"].append({"type": type(value).__name__, "value": value})
        except Exception as error:
            done["failed"] = {"index": index, "type": kind, "error": f"{type(error).__name__}: {str(error)[:200]}"}
            break
        done["performed"].append(kind)
    return done
