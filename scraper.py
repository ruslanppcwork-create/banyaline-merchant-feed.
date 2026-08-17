#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Banya Line -> Google Merchant Center XML feed

Source: https://banyaline.by
Output: merchant.xml

The script:
1) discovers /catalog/... URLs through sitemap and catalog crawling;
2) parses product data from JSON-LD / OpenGraph / HTML fallbacks;
3) generates an RSS 2.0 Google Merchant XML feed.
"""

from __future__ import annotations

import json
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from lxml import etree


BASE_URL = "https://banyaline.by"
CATALOG_URL = f"{BASE_URL}/catalog"
OUTPUT_FILE = "merchant.xml"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; BanyaLineMerchantFeed/1.0; "
        "+https://banyaline.by)"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.25
MAX_CRAWL_PAGES = 300

# Google Merchant RSS namespace
G_NS = "http://base.google.com/ns/1.0"

# We deliberately keep titles close to the landing-page titles.
BRAND = "Banya Line"
CURRENCY = "BYN"

# Product type classification for reporting / Merchant structure.
TYPE_RULES = [
    (("матрас",), "Баня и сауна > Матрасы"),
    (("валик",), "Баня и сауна > Валики"),
    (("подуш",), "Баня и сауна > Подушки"),
    (("веер",), "Баня и сауна > Вееры"),
    (("черпак", "ковш"), "Баня и сауна > Черпаки и ковши"),
    (("панно", "можжевел"), "Баня и сауна > Можжевеловые панно"),
    (("килт",), "Баня и сауна > Одежда > Килты"),
    (("парео",), "Баня и сауна > Одежда > Парео"),
    (("халат",), "Баня и сауна > Одежда > Халаты"),
    (("полотен",), "Баня и сауна > Текстиль > Полотенца"),
    (("простын",), "Баня и сауна > Текстиль > Простыни"),
    (("шорт",), "Баня и сауна > Одежда > Шорты"),
    (("костюм",), "Баня и сауна > Одежда > Костюмы"),
    (("набор", "комплект"), "Баня и сауна > Наборы"),
]

NON_PRODUCT_SLUGS = {
    "",
    "all",
    "matrasy",
    "matras",
    "valiki",
    "valik",
    "veera",
    "veer",
    "clothes",
    "odezhda",
    "accessories",
    "aksessuary",
    "kit",
    "kits",
    "nabory",
    "izdeliya",
    "kilt",
    "pareo",
    "halat",
    "polotence",
    "prostyni",
    "metall",
    "metal",
    "derevo",
    "podushki",
    "pillow",
}


session = requests.Session()
session.headers.update(HEADERS)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def get(url: str) -> requests.Response:
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    time.sleep(REQUEST_DELAY)
    return response


def canonicalize(url: str) -> str:
    parsed = urlparse(urljoin(BASE_URL, url))
    if parsed.netloc not in {"banyaline.by", "www.banyaline.by"}:
        return ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/")
    if not path:
        path = "/"
    return f"https://banyaline.by{path}"


def discover_from_sitemap() -> set[str]:
    urls = set()
    todo = [f"{BASE_URL}/sitemap.xml"]
    seen = set()

    while todo:
        sitemap = todo.pop()
        if sitemap in seen:
            continue
        seen.add(sitemap)

        try:
            response = get(sitemap)
        except Exception:
            continue

        try:
            root = etree.fromstring(response.content)
        except Exception:
            continue

        locs = root.xpath("//*[local-name()='loc']/text()")
        for loc in locs:
            loc = clean_text(loc)
            if not loc:
                continue
            if loc.lower().endswith(".xml"):
                todo.append(loc)
                continue
            url = canonicalize(loc)
            if url and url.startswith(f"{BASE_URL}/catalog/"):
                urls.add(url)

    return urls


def discover_by_crawling() -> set[str]:
    """Crawl only catalog-related pages and collect /catalog/... URLs."""
    discovered = set()
    queue = [CATALOG_URL]
    visited = set()

    while queue and len(visited) < MAX_CRAWL_PAGES:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        try:
            response = get(url)
        except Exception as exc:
            print(f"[WARN] Cannot crawl {url}: {exc}", file=sys.stderr)
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        for a in soup.find_all("a", href=True):
            found = canonicalize(a.get("href"))
            if not found:
                continue
            if found == CATALOG_URL or found.startswith(f"{BASE_URL}/catalog/"):
                discovered.add(found)
                if found not in visited and found not in queue:
                    queue.append(found)

    discovered.discard(CATALOG_URL)
    return discovered


def json_ld_objects(soup: BeautifulSoup):
    for script in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue

        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)


def find_product_jsonld(soup: BeautifulSoup) -> dict | None:
    for obj in json_ld_objects(soup):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if any(str(t).lower() == "product" for t in types if t):
            return obj
    return None


def meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        node = soup.find("meta", attrs={"property": name}) or soup.find(
            "meta", attrs={"name": name}
        )
        if node and node.get("content"):
            return clean_text(node["content"])
    return ""


def first_image_from_jsonld(product: dict) -> str:
    image = product.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, list) and image:
        first = image[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("url") or first.get("contentUrl") or ""
    if isinstance(image, dict):
        return image.get("url") or image.get("contentUrl") or ""
    return ""


def parse_price(value) -> str:
    if value is None:
        return ""
    text = clean_text(str(value)).replace("\xa0", " ")
    # 305,00 / 305.00 / 1 250
    match = re.search(r"(\d[\d\s]*[.,]?\d*)", text)
    if not match:
        return ""
    number = match.group(1).replace(" ", "").replace(",", ".")
    try:
        amount = float(number)
    except ValueError:
        return ""
    if amount <= 0:
        return ""
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def extract_offer(product: dict) -> dict:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    return offers if isinstance(offers, dict) else {}


def normalize_availability(raw: str, body_text: str) -> str:
    raw = (raw or "").lower()
    body = (body_text or "").lower()

    if "outofstock" in raw or "out_of_stock" in raw:
        return "out_of_stock"
    if "preorder" in raw or "pre_order" in raw:
        return "preorder"
    if "backorder" in raw or "back_order" in raw:
        return "backorder"
    if "instock" in raw or "in_stock" in raw:
        return "in_stock"

    if any(x in body for x in ("нет в наличии", "товар закончился", "распродано")):
        return "out_of_stock"

    # The public shop presents the product for ordering and no "out of stock"
    # signal was found.
    return "in_stock"


def classify_product(title: str, description: str, url: str) -> str:
    haystack = f"{title} {description} {url}".lower()
    for keywords, product_type in TYPE_RULES:
        if any(keyword in haystack for keyword in keywords):
            return product_type
    return "Баня и сауна > Аксессуары"


def slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1]


def make_id(url: str, sku: str = "") -> str:
    if sku:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", sku).strip("-")
        if normalized:
            return normalized[:50]
    slug = slug_from_url(url)
    if slug:
        return slug[:50]
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]


def parse_product(url: str) -> dict | None:
    try:
        response = get(url)
    except Exception as exc:
        print(f"[WARN] Cannot fetch {url}: {exc}", file=sys.stderr)
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    body_text = clean_text(soup.get_text(" ", strip=True))
    product = find_product_jsonld(soup) or {}
    offer = extract_offer(product)

    title = clean_text(product.get("name"))
    if not title:
        title = meta(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True) if h1 else "")

    description = clean_text(product.get("description"))
    if not description:
        description = meta(soup, "description", "og:description")
    if not description:
        # Keep the feed valid even if the page has no meta description.
        description = title

    image = first_image_from_jsonld(product)
    if not image:
        image = meta(soup, "og:image", "twitter:image")
    image = urljoin(url, image) if image else ""

    price = parse_price(
        offer.get("price")
        or offer.get("lowPrice")
        or product.get("price")
        or meta(soup, "product:price:amount")
    )

    currency = clean_text(
        offer.get("priceCurrency")
        or product.get("priceCurrency")
        or meta(soup, "product:price:currency")
        or CURRENCY
    ).upper()

    availability = normalize_availability(
        clean_text(offer.get("availability")), body_text
    )

    sku = clean_text(product.get("sku") or product.get("mpn"))
    brand = product.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    brand = clean_text(brand) or BRAND

    canonical = soup.find("link", rel="canonical")
    canonical_url = canonicalize(canonical.get("href")) if canonical else url
    canonical_url = canonical_url or url

    # Category/list pages usually have no price. Product pages must have
    # title + price + image for Merchant.
    if not title or not price or not image:
        return None

    # Avoid category landing pages that happen to contain aggregate price text.
    slug = slug_from_url(canonical_url).lower()
    if slug in NON_PRODUCT_SLUGS:
        return None

    return {
        "id": make_id(canonical_url, sku),
        "title": title[:150],
        "description": description[:5000],
        "link": canonical_url,
        "image_link": image,
        "availability": availability,
        "price": f"{price} {currency or CURRENCY}",
        "brand": brand,
        "condition": "new",
        "product_type": classify_product(title, description, canonical_url),
        # Do not invent GTIN/EAN/MPN. If manufacturer identifiers are later
        # assigned, this field should be changed and those identifiers added.
        "identifier_exists": "no",
    }


def add_g(item, name: str, value: str):
    node = etree.SubElement(item, f"{{{G_NS}}}{name}")
    node.text = value


def build_feed(products: list[dict]):
    nsmap = {"g": G_NS}
    rss = etree.Element("rss", version="2.0", nsmap=nsmap)
    channel = etree.SubElement(rss, "channel")

    etree.SubElement(channel, "title").text = "Banya Line"
    etree.SubElement(channel, "link").text = BASE_URL
    etree.SubElement(channel, "description").text = (
        "Google Merchant Center product feed for Banya Line"
    )
    etree.SubElement(channel, "lastBuildDate").text = (
        datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    )

    for product in sorted(products, key=lambda p: p["id"]):
        item = etree.SubElement(channel, "item")
        add_g(item, "id", product["id"])
        add_g(item, "title", product["title"])
        add_g(item, "description", product["description"])
        add_g(item, "link", product["link"])
        add_g(item, "image_link", product["image_link"])
        add_g(item, "availability", product["availability"])
        add_g(item, "price", product["price"])
        add_g(item, "brand", product["brand"])
        add_g(item, "condition", product["condition"])
        add_g(item, "product_type", product["product_type"])
        add_g(item, "identifier_exists", product["identifier_exists"])

    tree = etree.ElementTree(rss)
    tree.write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )


def main():
    print("Discovering product URLs...")
    sitemap_urls = discover_from_sitemap()
    crawl_urls = discover_by_crawling()
    candidates = sorted(sitemap_urls | crawl_urls)

    print(f"Candidate /catalog/ URLs: {len(candidates)}")

    products = []
    for index, url in enumerate(candidates, 1):
        product = parse_product(url)
        if product:
            products.append(product)
            print(
                f"[{index}/{len(candidates)}] PRODUCT: "
                f"{product['title']} — {product['price']}"
            )
        else:
            print(f"[{index}/{len(candidates)}] skip: {url}")

    # Remove accidental duplicates by ID; keep first occurrence.
    unique = {}
    for p in products:
        unique.setdefault(p["id"], p)
    products = list(unique.values())

    if not products:
        raise RuntimeError(
            "No products found. The site markup may have changed. "
            "Check GitHub Actions logs."
        )

    build_feed(products)
    print(f"Done. Products in feed: {len(products)}")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
