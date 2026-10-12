"""Navigation-led markdown (Jina Reader, Firecrawl markdown, ...) is not graded good."""
from frankensurf.completeness import assess, nav_led, quality

URL = "https://news.example.com/world/harbour-ferry-launches"
SENTENCE = ("The new harbour ferry carries commuters across the water in twenty minutes, and the city says "
            "the service will run every day of the week.")
ARTICLE = "\n\n".join(f"Paragraph {n}. {SENTENCE} {SENTENCE}" for n in range(12))
NAV = "\n".join(f"* [Section {n} of the site](https://news.example.com/s/{n})" for n in range(70))
HEADER = "[Skip to content](#main)\n\nMenu\n\nSign in\n\nSubscribe\n\n" + NAV
THIN = "\n\n".join([f"{SENTENCE} {SENTENCE}"] * 3)
COOKIES = "We use cookies to personalise content and ads, so please accept all or manage your preferences here."
FOOTER = "\n".join(f"[Footer link {n}](https://news.example.com/f/{n})" for n in range(20))


def grade(markdown, url=URL):
    result = {"content": markdown, "text": markdown, "content_type": "text/markdown; charset=utf-8"}
    verdict = assess(url, result)
    return verdict, quality(verdict)["grade"]


def test_navigation_only_page_is_poor():
    verdict, grade_ = grade(f"Title: Ferry\n\n{HEADER}\n\n{COOKIES}\n\n{FOOTER}\n")
    assert verdict["complete"] is False and verdict["nav_led"]
    assert grade_ == "poor" and "navigation" in quality(verdict)["flags"]


def test_navigation_then_thin_prose_is_poor():
    verdict, grade_ = grade(f"Title: Ferry\n\n{HEADER}\n\n# Ferry\n\n{THIN}\n\n{FOOTER}\n")
    assert verdict["complete"] is False and "prose follow" in verdict["reason"] and grade_ == "poor"


def test_navigation_then_a_long_article_stays_good():
    verdict, grade_ = grade(f"Title: Ferry\n\n{HEADER}\n\n{COOKIES}\n\n# Ferry\n\n{ARTICLE}\n\n{FOOTER}\n")
    assert verdict["complete"] is True and grade_ == "good" and not verdict.get("nav_led")


def test_a_normal_article_is_good():
    assert grade(f"# Ferry launches\n\nBy A. Writer\n\n{ARTICLE}\n")[1] == "good"


def test_a_docs_page_with_a_sidebar_then_content_is_good():
    sidebar = "\n".join(f"- [Guide chapter {n}](/docs/guide/{n})" for n in range(60))
    docs = f"# Install\n\n{sidebar}\n\n## Steps\n\n{ARTICLE}\n\n```\npip install thing\n```\n"
    verdict, grade_ = grade(docs, "https://docs.example.com/guide/install")
    assert verdict["complete"] is True and grade_ == "good"


def test_a_requested_index_page_is_ok():
    index = "# All articles\n\n" + "\n".join(
        f"* [Article {n}: a long and descriptive title about harbour ferries](https://news.example.com/a/{n})"
        for n in range(80))
    assert grade(index, "https://news.example.com/archive/")[1] == "good"
    assert grade(index, "https://news.example.com/search?q=ferry")[0].get("nav_led") is None


def test_html_and_short_prose_pages_are_untouched():
    html = {"content": "<p>" + SENTENCE * 20 + "</p>", "text": SENTENCE * 20, "content_type": "text/html"}
    assert assess(URL, html)["complete"] is True
    assert nav_led(f"# Notes\n\n{ARTICLE}") is None
