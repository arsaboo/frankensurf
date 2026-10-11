"""Image URLs from a page never break the read, whatever shape its structured data takes."""
from frankensurf.runtime import _image_urls


def test_nested_and_odd_image_values_are_skipped_not_fatal():
    structured = {"json_ld": [{"@type": "NewsArticle",
                               "image": {"@type": "ImageObject", "url": {"@id": "https://x.example/a.jpg"}}},
                              {"image": [{"url": ["https://x.example/b.jpg"]}, "/c.jpg", 7, None]},
                              {"images": {"src": "https://x.example/d.jpg"}}]}
    assert _image_urls(structured, None, "https://x.example/page") == [
        "https://x.example/c.jpg", "https://x.example/d.jpg"]
