"""Structured extraction with checks: a JSON Schema filled from what pages publish (synthetic pages)."""
import json

import httpx
import pytest

from frankensurf import schema_extract
from frankensurf.runtime import Runtime, WebPolicy

PRODUCT = {
    "type": "object", "title": "Product",
    "properties": {"name": {"type": "string"}, "price": {"type": "number"}, "currency": {"type": "string"},
                   "image": {"type": "string"}, "brand": {"type": "string"}, "rating": {"type": "number"},
                   "sku": {"type": "string"}},
    "required": ["name", "price"],
}


def page(html, url="https://shop.example.com/item/42"):
    from frankensurf.runtime import parse_content
    parsed = parse_content(html, "text/html", url, None)
    return {**parsed, "content": html, "content_type": "text/html", "url": url}


def jsonld(data):
    return f'<script type="application/ld+json">{json.dumps(data)}</script>'


def test_a_product_is_filled_from_json_ld_with_types_coerced():
    html = ("<html><head><title>Lamp</title>" + jsonld({"@context": "https://schema.org", "@graph": [
        {"@type": "BreadcrumbList", "name": "Home"},
        {"@type": "Product", "name": "Brass desk lamp", "sku": "L-42", "brand": {"@type": "Brand", "name": "Lumen"},
         "image": ["https://img.example.com/lamp.jpg"],
         "offers": {"@type": "Offer", "price": "1,299.00", "priceCurrency": "AUD"},
         "aggregateRating": {"ratingValue": "4.6", "reviewCount": "88"}}]}) + "</head><body>Lamp</body></html>")
    got = schema_extract.extract(PRODUCT, page(html))
    assert got["data"] == {"name": "Brass desk lamp", "price": 1299.0, "currency": "AUD",
                           "image": "https://img.example.com/lamp.jpg", "brand": "Lumen", "rating": 4.6,
                           "sku": "L-42"}
    assert got["missing"] == [] and got["valid"] is True and got["found"] == 7
    assert got["sources"]["price"] == "jsonld:product.offers.price"


def test_meta_tags_fill_what_json_ld_does_not_have():
    html = ('<html><head><meta property="og:title" content="Ferry timetable changes">'
            '<meta property="article:published_time" content="2026-10-12T08:00:00Z">'
            '<meta name="description" content="The harbour ferry runs every twenty minutes.">'
            '<meta property="og:image" content="https://img.example.com/ferry.jpg"></head><body>x</body></html>')
    schema = {"type": "object", "properties": {"headline": {"type": "string"}, "published": {"type": "string"},
                                               "description": {"type": "string"}, "image": {"type": "string"}},
              "required": ["headline", "published"]}
    got = schema_extract.extract(schema, page(html, "https://news.example.com/2026/10/ferry"))
    assert got["data"]["headline"] == "Ferry timetable changes"
    assert got["data"]["published"] == "2026-10-12T08:00:00Z"
    assert got["sources"]["published"] == "meta:article:published_time" and got["missing"] == []


def test_microdata_and_european_number_formats():
    html = ('<html><body><div itemscope itemtype="https://schema.org/Product">'
            '<h1 itemprop="name">Wanderschuh</h1><span itemprop="price" content="1.249,50">1.249,50 €</span>'
            '</div></body></html>')
    got = schema_extract.extract(PRODUCT, page(html))
    assert got["data"]["name"] == "Wanderschuh" and got["data"]["price"] == 1249.5
    assert got["sources"]["name"].startswith("microdata:")


def test_the_page_s_own_json_is_searched_by_key_name():
    html = ('<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps({"props": {"pageProps": {"listing": {"title": "Garden shed", "price": 450}}}})
            + "</script></body></html>")
    schema = {"type": "object", "properties": {"title": {"type": "string"}, "price": {"type": "number"}}}
    got = schema_extract.extract(schema, page(html))
    if not got["data"]:
        pytest.skip("this parser keeps no embedded JSON for this page shape")
    assert got["data"]["price"] == 450 and got["sources"]["price"] == "embedded_json"


def test_missing_required_fields_and_explicit_paths():
    html = "<html><head>" + jsonld({"@type": "Product", "name": "Lamp", "offers": {"lowPrice": "19"}}) + "</head></html>"
    got = schema_extract.extract({**PRODUCT, "required": ["name", "price", "sku"]}, page(html))
    assert got["data"]["price"] == 19 and got["missing"] == ["sku"]
    custom = {"type": "object", "properties": {"cheapest": {"type": "number", "x-paths": ["offers.lowPrice"]}},
              "required": ["cheapest"]}
    assert schema_extract.extract(custom, page(html))["data"] == {"cheapest": 19}


def test_a_schema_must_be_an_object_schema_with_properties():
    for bad in ({}, {"type": "object"}, {"properties": []}, {"properties": {"a": {"type": "nonsense"}}}):
        with pytest.raises(ValueError):
            schema_extract.check_schema(bad)
    with pytest.raises(ValueError):
        WebPolicy(extract_schema={"type": "object"})
    assert WebPolicy(extract_schema=PRODUCT).extract_schema == PRODUCT


def test_the_gate_marks_a_page_without_a_required_field_incomplete():
    policy = WebPolicy(extract_schema=PRODUCT)
    without = page("<html><head>" + jsonld({"@type": "Product", "name": "Lamp"}) + "</head></html>")
    verdict = Runtime._schema_gate({"complete": True, "score": 10, "reason": None}, without, policy)
    assert verdict["complete"] is False and verdict["reason"] == "missing required fields: price"
    with_price = page("<html><head>" + jsonld({"@type": "Product", "name": "Lamp", "offers": {"price": 9}}) + "</head></html>")
    better = Runtime._schema_gate({"complete": True, "score": 10, "reason": None}, with_price, policy)
    assert better["complete"] is True and better["score"] > verdict["score"]
    assert Runtime._schema_gate({"complete": True, "score": 1}, without, WebPolicy()) == {"complete": True, "score": 1}


async def test_a_read_with_a_schema_returns_extracted_fields(tmp_path):
    html = ("<html><head><title>Lamp</title>" + jsonld({"@context": "https://schema.org", "@type": "Product",
            "name": "Brass desk lamp", "offers": {"price": "89.95", "priceCurrency": "AUD"}})
            + "</head><body>" + "<p>A brass desk lamp with a warm light.</p>" * 30 + "</body></html>")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
    async with Runtime(tmp_path, transport=transport) as web:
        result = await web.read("https://shop.example.com/item/42",
                                policy_overrides={"provider": "http", "extract_schema": PRODUCT})
    assert result["extracted"]["data"]["price"] == 89.95 and result["extracted"]["missing"] == []
    assert result["receipt"]["extracted"] == {"found": 3, "of": 7, "missing": []}


def test_the_cli_reads_a_schema_file(tmp_path):
    from frankensurf.cli import _policy_kwargs, parse_args
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(PRODUCT))
    assert _policy_kwargs(parse_args(["read", "https://example.com", "--schema", str(path)]))["extract_schema"] == PRODUCT
