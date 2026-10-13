"""Structured extraction with checks: fill a JSON Schema from the data a page publishes.

read(url, policy_overrides={"extract_schema": schema}) returns result["extracted"]:
{"data": {...}, "missing": [...], "sources": {field: where}, "valid": bool, "errors": [...]}.

No model is involved. Values come from what pages publish for machines: JSON-LD (schema.org),
microdata (itemprop), Open Graph and other meta tags, and the page's own embedded JSON. A schema
property is matched by its name and its synonyms (price: offers.price, lowPrice,
product:price:amount; published: datePublished, article:published_time ...), coerced to its type
(a "$1,299.00" string to 1299.0 for a number), and the whole object is validated against the
schema. A property may name its own paths with "x-paths": ["offers.price", "meta:og:price"].

A page whose required properties are not all found counts as incomplete, so an automatic read
climbs to a stronger tool, as it does for a search page with no results (Runtime._ensure_complete).
Generic: no site names, only schema.org vocabulary and common meta tags.
"""
from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

# Property name (normalised) -> other names that carry it, most specific first. Dotted names are
# paths inside a JSON-LD object; "meta:" names are meta tags (property= or name=).
SYNONYMS = {
    "name": ["name", "headline", "title", "meta:og:title", "meta:twitter:title"],
    "title": ["headline", "name", "title", "meta:og:title", "meta:twitter:title"],
    "headline": ["headline", "name", "meta:og:title"],
    "description": ["description", "abstract", "meta:og:description", "meta:description",
                    "meta:twitter:description"],
    "price": ["offers.price", "offers.lowPrice", "offers.priceSpecification.price", "price", "lowPrice",
              "meta:product:price:amount", "meta:og:price:amount"],
    "currency": ["offers.priceCurrency", "priceCurrency", "offers.priceSpecification.priceCurrency",
                 "meta:product:price:currency", "meta:og:price:currency"],
    "pricecurrency": ["offers.priceCurrency", "priceCurrency", "meta:product:price:currency"],
    "availability": ["offers.availability", "availability", "meta:product:availability"],
    "image": ["image.url", "image", "thumbnailUrl", "meta:og:image", "meta:twitter:image"],
    "images": ["image", "image.url", "meta:og:image"],
    "url": ["url", "mainEntityOfPage.@id", "@id", "meta:og:url"],
    "brand": ["brand.name", "brand", "manufacturer.name"],
    "sku": ["sku", "mpn", "productID"],
    "gtin": ["gtin13", "gtin", "gtin12", "gtin14", "gtin8"],
    "rating": ["aggregateRating.ratingValue", "reviewRating.ratingValue"],
    "ratingcount": ["aggregateRating.ratingCount", "aggregateRating.reviewCount"],
    "reviewcount": ["aggregateRating.reviewCount", "aggregateRating.ratingCount"],
    "author": ["author.name", "author", "creator.name", "meta:author", "meta:article:author"],
    "published": ["datePublished", "uploadDate", "dateCreated", "meta:article:published_time",
                  "meta:og:article:published_time", "meta:date"],
    "datepublished": ["datePublished", "uploadDate", "meta:article:published_time"],
    "modified": ["dateModified", "meta:article:modified_time", "meta:og:updated_time"],
    "updated": ["dateModified", "meta:article:modified_time", "meta:og:updated_time"],
    "publisher": ["publisher.name", "publisher", "meta:og:site_name"],
    "sitename": ["meta:og:site_name", "publisher.name"],
    "category": ["category", "articleSection", "meta:article:section"],
    "keywords": ["keywords", "meta:keywords", "meta:article:tag"],
    "location": ["location.name", "location.address.addressLocality", "address.addressLocality",
                 "jobLocation.address.addressLocality", "contentLocation.name"],
    "address": ["address.streetAddress", "location.address.streetAddress", "address"],
    "city": ["address.addressLocality", "location.address.addressLocality",
             "jobLocation.address.addressLocality"],
    "country": ["address.addressCountry", "location.address.addressCountry",
                "jobLocation.address.addressCountry"],
    "telephone": ["telephone", "contactPoint.telephone"],
    "email": ["email", "contactPoint.email"],
    "startdate": ["startDate"],
    "enddate": ["endDate"],
    "company": ["hiringOrganization.name", "organization.name", "publisher.name"],
    "salary": ["baseSalary.value.value", "baseSalary.value.minValue", "baseSalary.value", "estimatedSalary"],
    "duration": ["duration", "timeRequired"],
    "language": ["inLanguage", "meta:og:locale"],
}
# Values that look like a property but say nothing.
_EMPTY = {"", "null", "none", "n/a", "undefined", "-"}
_NUMBER = re.compile(r"-?\d(?:[\d.,]|\s(?=\d{3}\b))*\d|-?\d")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _objects(value, found, depth=0):
    """Every dict inside JSON-LD (lists and @graph unrolled), outermost first."""
    if depth > 6:
        return
    if isinstance(value, list):
        for item in value:
            _objects(item, found, depth + 1)
    elif isinstance(value, dict):
        found.append(value)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in value:
                _objects(value[key], found, depth + 1)


def _types(obj) -> set[str]:
    kind = obj.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return {str(item).lower() for item in kinds if item}


def _path(obj, path: str):
    """A dotted path inside a JSON-LD object; lists take their first usable element."""
    value = obj
    for part in path.split("."):
        if isinstance(value, list):
            value = next((item for item in value if item not in (None, "", [], {})), None)
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _meta(soup) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").strip().lower()
        value = (tag.get("content") or "").strip()
        if key and value:
            tags.setdefault(key, []).append(value)
    return tags


def _microdata(soup) -> list[dict]:
    """Top-level itemscope blocks as flat dicts of itemprop -> text (one level of nesting)."""
    blocks = []
    for scope in soup.select("[itemscope]")[:50]:
        if scope.find_parent(attrs={"itemscope": True}):
            continue
        item: dict[str, Any] = {"@type": (scope.get("itemtype") or "").rsplit("/", 1)[-1], "@source": "microdata"}
        for prop in scope.select("[itemprop]")[:200]:
            name = prop.get("itemprop")
            value = (prop.get("content") or prop.get("datetime") or prop.get("href") or prop.get("src")
                     or prop.get_text(" ", strip=True))
            if name and value and name not in item:
                item[name] = value
        blocks.append(item)
    return blocks


def _sources(result: dict):
    """(JSON-LD and microdata objects, meta tags, embedded JSON) from a read result."""
    structured = result.get("structured") if isinstance(result.get("structured"), dict) else {}
    objects: list[dict] = []
    _objects(structured.get("jsonld") or [], objects)
    content = result.get("content") or ""
    meta: dict[str, list[str]] = {}
    if "<" in content[:5000] and ("html" in str(result.get("content_type") or "html")):
        soup = BeautifulSoup(content[:3_000_000], "html.parser")
        meta = _meta(soup)
        objects += _microdata(soup)
    embedded = structured.get("embedded_json")
    return objects, meta, embedded


def _candidates(prop_name: str, spec: dict) -> list[str]:
    paths = list(spec.get("x-paths") or [])
    paths += SYNONYMS.get(_norm(prop_name), [])
    if prop_name not in paths:
        paths.append(prop_name)
    return list(dict.fromkeys(paths))


def _scalar(value):
    if isinstance(value, dict):
        for key in ("name", "@value", "value", "url", "@id", "text"):
            if value.get(key) not in (None, ""):
                return _scalar(value[key])
        return None
    if isinstance(value, list):
        return _scalar(next((item for item in value if item not in (None, "", [], {})), None))
    return value


def _coerce(value, spec: dict):
    """The value as the schema property's type, or None when it cannot be."""
    kind = spec.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    if "array" in kinds:
        items = value if isinstance(value, list) else [value]
        inner = spec.get("items") if isinstance(spec.get("items"), dict) else {"type": "string"}
        out = [item for item in (_coerce(item, inner) for item in items) if item is not None]
        return out or None
    value = _scalar(value)
    if value is None or (isinstance(value, str) and value.strip().lower() in _EMPTY):
        return None
    if "number" in kinds or "integer" in kinds:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = value
        else:
            match = _NUMBER.search(str(value))
            if not match:
                return None
            text = re.sub(r"\s", "", match.group(0))
            # 1,299.00 -> 1299.00; 1.299,00 -> 1299.00; 12,5 -> 12.5; 1.234.567 -> 1234567
            if "," in text and "." in text:
                text = text.replace(",", "") if text.rfind(".") > text.rfind(",") else text.replace(".", "").replace(",", ".")
            elif "," in text:
                text = text.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", text) else text.replace(",", ".")
            elif text.count(".") > 1:
                text = text.replace(".", "")
            try:
                number = float(text)
            except ValueError:
                return None
        return int(number) if "integer" in kinds and float(number).is_integer() else (
            number if "number" in kinds else None)
    if "boolean" in kinds:
        text = str(value).strip().lower()
        return True if text in ("true", "yes", "1", "instock") else False if text in ("false", "no", "0") else None
    if kinds == [None] or "string" in kinds:
        text = " ".join(str(value).split())
        return text or None
    return value


def _find(prop_name, spec, objects, meta, embedded, preferred):
    for path in _candidates(prop_name, spec):
        if path.startswith("meta:"):
            values = meta.get(path[5:].lower())
            if values:
                coerced = _coerce(values if spec.get("type") == "array" else values[0], spec)
                if coerced is not None:
                    return coerced, path
            continue
        for obj in preferred + [o for o in objects if o not in preferred]:
            coerced = _coerce(_path(obj, path), spec)
            if coerced is not None:
                kind = next(iter(_types(obj)), "") or "object"
                return coerced, f"{obj.get('@source', 'jsonld')}:{kind}.{path}"
    if embedded is not None:
        value = _deep_key(embedded, {_norm(path.rsplit(".", 1)[-1]) for path in _candidates(prop_name, spec)
                                     if not path.startswith("meta:")})
        coerced = _coerce(value, spec) if value is not None else None
        if coerced is not None:
            return coerced, "embedded_json"
    return None, None


def _deep_key(value, names, depth=0, budget=None):
    """The first scalar under a key with one of these names, breadth-limited."""
    budget = budget if budget is not None else [20000]
    if depth > 12 or budget[0] <= 0:
        return None
    budget[0] -= 1
    if isinstance(value, dict):
        for key, item in value.items():
            if _norm(key) in names and not isinstance(item, (dict, list)) and item not in (None, ""):
                return item
        for item in value.values():
            found = _deep_key(item, names, depth + 1, budget)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value[:200]:
            found = _deep_key(item, names, depth + 1, budget)
            if found is not None:
                return found
    return None


def check_schema(schema) -> None:
    """Raise ValueError unless schema is a usable object schema."""
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict) or not schema["properties"]:
        raise ValueError("extract_schema must be a JSON Schema object with properties")
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise ValueError("extract_schema is not a valid JSON Schema: " + error.message) from None


def extract(schema: dict, result: dict) -> dict:
    """Fill the schema's properties from the page's published data; see the module docstring."""
    from jsonschema import Draft202012Validator
    objects, meta, embedded = _sources(result)
    wanted = {str(t).lower() for t in ([schema.get("x-type")] if isinstance(schema.get("x-type"), str)
                                       else schema.get("x-type") or [])}
    if not wanted and isinstance(schema.get("title"), str):
        wanted = {schema["title"].lower()}
    preferred = [obj for obj in objects if _types(obj) & wanted]
    if not preferred and objects:
        # The object that fills the most properties is the page's main thing.
        def fills(obj):
            return sum(_coerce(_path(obj, path), spec) is not None
                       for name, spec in schema["properties"].items()
                       for path in _candidates(name, spec if isinstance(spec, dict) else {})[:3]
                       if not path.startswith("meta:"))
        preferred = [max(objects, key=fills)]
    data, sources = {}, {}
    for name, spec in schema["properties"].items():
        spec = spec if isinstance(spec, dict) else {}
        value, where = _find(name, spec, objects, meta, embedded, preferred)
        if value is not None:
            data[name] = value
            sources[name] = where
    required = [name for name in schema.get("required") or [] if name in schema["properties"]]
    missing = [name for name in required if name not in data]
    errors = [error.message for error in Draft202012Validator(schema).iter_errors(data)][:10]
    return {"data": data, "missing": missing, "sources": sources, "valid": not errors,
            "errors": errors, "found": len(data), "of": len(schema["properties"])}
