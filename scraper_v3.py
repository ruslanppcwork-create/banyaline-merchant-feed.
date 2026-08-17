#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
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
        "Mozilla/5.0 (compatible; BanyaLineMerchantFeed/2.0; "
        "+https://banyaline.by)"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
}

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.20
MAX_CRAWL_PAGES = 300

G_NS = "http://base.google.com/ns/1.0"
BRAND = "Banya Line"
CURRENCY = "BYN"

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
    (("запарк",), "Баня и сауна > Запарки"),
]

# URLs that are category/landing pages, not individual product cards.
NON_PRODUCT_SLUGS = {
    "",
    "all",
    "catalog",
    "matras",
    "matrasy",
    "valik",
    "valiki",
    "veer",
    "veera",
    "pareo",
    "pillow",
    "podushka",
    "podushki",
    "polotence",
    "polotenca",
    "prostyn",
    "prostyni",
    "kilt",
    "kilts",
    "halat",
    "halaty",
    "metall",
    "metal",
    "scoop",
    "ladle",
    "ventilation",
    "zaparka",
    "zaparki",
    "clothes",
    "odezhda",
    "accessories",
    "aksessuary",
    "kit",
    "kits",
    "nabory",
    "izdeliya",
    "derevo",
}

session = requests.Session()
session.headers.update(HEADERS)


def clean_text(value) -> str:
    if value is None:
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
            root = etree.fromstring(response.content)
        except Exception:
            continue

        for loc in root.xpath("//*[local-name()='loc']/text()"):
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
    for script in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
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


def find_product_jsonld(soup: BeautifulSoup) -> dict:
    for obj in json_ld_objects(soup):
        typ = obj.get("@type")
        types = typ if isinstance(typ, list) else [typ]
        if any(str(t).lower() == "product" for t in types if t):
            return obj
    return {}


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


def parse_number(value) -> str:
    if value is None:
        return ""
    text = clean_text(value).replace("\xa0", " ")
    match = re.search(r"(\d[\d\s]*(?:[.,]\d{1,2})?)", text)
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


def extract_price(product: dict, soup: BeautifulSoup, html: str, body_text: str) -> str:
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        offers = {}

    candidates = [
        offers.get("price"),
        offers.get("lowPrice"),
        product.get("price"),
        meta(soup, "product:price:amount"),
    ]

    # Tilda often stores price in data attributes / JS rather than Product JSON-LD.
    raw_patterns = [
        r'data-product-price=["\']\s*([^"\']+)',
        r'data-product-price-def=["\']\s*([^"\']+)',
        r'["\']price["\']\s*:\s*["\']?(\d+(?:[.,]\d+)?)',
        r'["\']amount["\']\s*:\s*["\']?(\d+(?:[.,]\d+)?)',
    ]
    for pattern in raw_patterns:
        m = re.search(pattern, html, flags=re.I)
        if m:
            candidates.append(m.group(1))

    # Final fallback: visible page text such as "90 BYN".
    visible_prices = re.findall(
        r'(?<!\d)(\d{1,6}(?:[ \xa0]\d{3})*(?:[.,]\d{1,2})?)\s*BYN\b',
        body_text,
        flags=re.I,
    )
    candidates.extend(visible_prices)

    for candidate in candidates:
        parsed = parse_number(candidate)
        if parsed:
            return parsed
    return ""


def normalize_image_url(value: str, page_url: str) -> str:
    value = clean_text(value)
    if not value:
        return ""

    # Tilda may keep one or several photos in JSON-like attributes.
    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list) and parsed:
                first = parsed[0]
                if isinstance(first, dict):
                    value = first.get("img") or first.get("url") or first.get("src") or ""
                elif isinstance(first, str):
                    value = first
        except Exception:
            pass

    # Sometimes several URLs are packed into one attribute.
    if "|||" in value:
        value = value.split("|||", 1)[0]
    elif ";" in value and "http" in value:
        value = value.split(";", 1)[0]

    value = value.strip().strip('"').strip("'")
    if value.startswith("//"):
        value = "https:" + value

    return urljoin(page_url, value) if value else ""


def is_valid_product_image(url: str) -> bool:
    lower = (url or "").lower()
    if not url:
        return False
    if any(x in lower for x in ("logo", "favicon", "/icons/", ".svg")):
        return False
    return "tildacdn" in lower or lower.startswith("http")


def extract_tilda_product_image(soup: BeautifulSoup, html: str, page_url: str) -> str:
    slug = slug_from_url(page_url).lower()

    # 1. Best source: Tilda's own product data attributes.
    # Prefer a product node that points to the current product URL.
    product_nodes = []
    for node in soup.find_all(attrs={"data-product-img": True}):
        score = 0
        node_url = clean_text(
            node.get("data-product-url")
            or node.get("data-product-link")
            or node.get("href")
        ).lower()

        if slug and slug in node_url:
            score += 100

        classes = " ".join(node.get("class", []))
        if "js-product" in classes or "t-store" in classes:
            score += 10

        product_nodes.append((score, node))

    for _, node in sorted(product_nodes, key=lambda x: x[0], reverse=True):
        image = normalize_image_url(node.get("data-product-img"), page_url)
        if is_valid_product_image(image):
            return image

    # 2. Other Tilda product image attributes.
    for attr in (
        "data-product-image",
        "data-product-photo",
        "data-product-gallery-img",
    ):
        for node in soup.find_all(attrs={attr: True}):
            image = normalize_image_url(node.get(attr), page_url)
            if is_valid_product_image(image):
                return image

    # 3. Raw HTML fallback for data-product-img.
    patterns = [
        r'data-product-img=["\']([^"\']+)["\']',
        r'data-product-image=["\']([^"\']+)["\']',
        r'data-product-photo=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        for match in re.findall(pattern, html, flags=re.I):
            image = normalize_image_url(match, page_url)
            if is_valid_product_image(image):
                return image

    # 4. Look inside product/gallery blocks only, not across the whole page.
    product_selectors = [
        ".js-product",
        ".t-store__prod-popup__slider",
        ".t-store__prod-popup__gallery",
        ".t-store__product-snippet",
        ".t-slds",
    ]
    for selector in product_selectors:
        for block in soup.select(selector):
            # <img> lazy loading
            for img in block.find_all("img"):
                for attr in ("data-original", "data-src", "src"):
                    image = normalize_image_url(img.get(attr), page_url)
                    if is_valid_product_image(image):
                        return image

            # background-image nodes often used by Tilda sliders
            for node in block.find_all(True):
                for attr in ("data-original", "data-bg", "data-img-zoom-url"):
                    image = normalize_image_url(node.get(attr), page_url)
                    if is_valid_product_image(image):
                        return image

    return ""


def extract_image(product: dict, soup: BeautifulSoup, html: str, page_url: str) -> str:
    # Product structured data is reliable when present.
    image = normalize_image_url(first_image_from_jsonld(product), page_url)
    if is_valid_product_image(image):
        return image

    # Tilda-specific product image must be checked BEFORE og:image.
    image = extract_tilda_product_image(soup, html, page_url)
    if is_valid_product_image(image):
        return image

    # og:image on this site can be a generic site-wide image, so use it only
    # as the last fallback.
    image = normalize_image_url(meta(soup, "og:image", "twitter:image"), page_url)
    if is_valid_product_image(image):
        return image

    return ""


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
    slug = slug_from_url(url).lower()
    if slug in NON_PRODUCT_SLUGS:
        print(f"  category landing: {url}")
        return None

    try:
        response = get(url)
    except Exception as exc:
        print(f"[WARN] Cannot fetch {url}: {exc}", file=sys.stderr)
        return None

    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    body_text = clean_text(soup.get_text(" ", strip=True))
    product = find_product_jsonld(soup)
    offer = extract_offer(product)

    title = clean_text(product.get("name"))
    if not title:
        title = meta(soup, "og:title", "twitter:title")
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ", strip=True) if h1 else "")

    # Clean common site-name suffixes from social titles.
    title = re.sub(r"\s*[-—|]\s*Banya\s*Line\s*$", "", title, flags=re.I).strip()

    description = clean_text(product.get("description"))
    if not description:
        description = meta(soup, "description", "og:description")
    if not description:
        # Try a meaningful H2/H3/paragraph before falling back to title.
        paragraph = soup.find("p")
        description = clean_text(paragraph.get_text(" ", strip=True) if paragraph else "")
    if not description:
        description = title

    image = extract_image(product, soup, html, url)
    price = extract_price(product, soup, html, body_text)

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

    missing = []
    if not title:
        missing.append("title")
    if not price:
        missing.append("price")
    if not image:
        missing.append("image")

    if missing:
        print(f"  skip reason={','.join(missing)} url={url}")
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

    unique = {}
    for p in products:
        unique.setdefault(p["id"], p)
    products = list(unique.values())

    if not products:
        raise RuntimeError(
            "No products found. See 'skip reason=' lines above."
        )

    # Safety check: if one image was accidentally assigned to a large share
    # of different products, do not publish a broken Merchant feed.
    image_counts = {}
    for p in products:
        image_counts[p["image_link"]] = image_counts.get(p["image_link"], 0) + 1

    most_common_image, most_common_count = max(
        image_counts.items(), key=lambda kv: kv[1]
    )

    if len(products) >= 10 and most_common_count / len(products) > 0.35:
        raise RuntimeError(
            "Image extraction looks wrong: the same image is used for "
            f"{most_common_count}/{len(products)} products: {most_common_image}"
        )

    print(
        f"Image check OK. Unique images: {len(image_counts)} / "
        f"{len(products)} products"
    )

    build_feed(products)
    print(f"Done. Products in feed: {len(products)}")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
