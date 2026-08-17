#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Banya Line -> Google Merchant Center feed, v5.

Important difference from earlier versions:
- does NOT crawl every product URL;
- reads Tilda product cards from the catalog/category pages;
- therefore makes only a small number of requests;
- product images are taken from the same Tilda product card as title/price/link.
"""

from __future__ import annotations

import json
import random
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
OUTPUT_FILE = "merchant.xml"
G_NS = "http://base.google.com/ns/1.0"

BRAND = "Banya Line"
CURRENCY = "BYN"

# First try the main catalog. If it already contains enough product cards,
# no section pages will be requested.
CATALOG_PAGES = [
    "/catalog",
    "/catalog/matras",
    "/catalog/valik",
    "/catalog/veer",
    "/catalog/kit",
    "/catalog/kilt",
    "/catalog/pareo",
    "/catalog/halat",
    "/catalog/polotence",
    "/catalog/prostyn",
    "/catalog/metal",
    "/catalog/derevo",
    "/catalog/pillow",
]

BAD_IMAGE_FILENAMES = {
    "frame_755829.jpg",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/139.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

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
    (("вешал", "полк"), "Баня и сауна > Аксессуары"),
    (("запарк",), "Баня и сауна > Запарки"),
]

session = requests.Session()
session.headers.update(HEADERS)


def clean_text(value) -> str:
    if value is None:
        return ""
    value = unescape(str(value))
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def canonical_url(value: str) -> str:
    if not value:
        return ""
    url = urljoin(BASE_URL, value)
    p = urlparse(url)
    if p.netloc not in {"banyaline.by", "www.banyaline.by"}:
        return ""
    path = re.sub(r"/+", "/", p.path).rstrip("/")
    if not path.startswith("/catalog/"):
        return ""
    return f"https://banyaline.by{path}"


def fetch(url: str) -> str:
    """
    A small number of browser-like requests with conservative retry.
    403 is usually temporary anti-bot/rate limiting; retry slowly rather
    than hammering the site.
    """
    waits = [0, 8, 20]
    last_error = None

    for attempt, wait in enumerate(waits, 1):
        if wait:
            print(f"Waiting {wait}s before retry {attempt} for {url}...")
            time.sleep(wait)

        try:
            headers = {
                "Referer": BASE_URL + "/",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-User": "?1",
            }
            response = session.get(
                url,
                headers=headers,
                timeout=30,
                allow_redirects=True,
            )

            if response.status_code == 200 and len(response.text) > 1000:
                return response.text

            last_error = RuntimeError(
                f"HTTP {response.status_code}, body={len(response.text)} bytes"
            )
            print(
                f"[WARN] {url}: HTTP {response.status_code} "
                f"(attempt {attempt}/{len(waits)})",
                file=sys.stderr,
            )

            # Do not rapidly retry normal client errors other than 403/429.
            if response.status_code not in {403, 429, 500, 502, 503, 504}:
                break

        except Exception as exc:
            last_error = exc
            print(
                f"[WARN] {url}: {exc} "
                f"(attempt {attempt}/{len(waits)})",
                file=sys.stderr,
            )

    raise RuntimeError(f"Cannot fetch {url}: {last_error}")


def parse_number(value) -> str:
    text = clean_text(value).replace("\xa0", " ")
    m = re.search(r"(\d[\d\s]*(?:[.,]\d{1,2})?)", text)
    if not m:
        return ""
    raw = m.group(1).replace(" ", "").replace(",", ".")
    try:
        number = float(raw)
    except ValueError:
        return ""
    if number <= 0:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def image_filename(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1].lower()


def normalize_image(value: str, page_url: str) -> str:
    if not value:
        return ""

    value = unescape(str(value)).replace("\\/", "/").strip()

    # Tilda may store the gallery as JSON.
    if value.startswith("["):
        try:
            data = json.loads(value)
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, dict):
                    value = (
                        first.get("img")
                        or first.get("url")
                        or first.get("src")
                        or first.get("original")
                        or ""
                    )
                elif isinstance(first, str):
                    value = first
        except Exception:
            pass

    if "|||" in value:
        value = value.split("|||", 1)[0]

    value = value.strip().strip('"').strip("'")
    if value.startswith("//"):
        value = "https:" + value
    if not value:
        return ""

    result = urljoin(page_url, value)
    lower = result.lower()

    if image_filename(result) in BAD_IMAGE_FILENAMES:
        return ""
    if any(x in lower for x in ("favicon", "logo", ".svg", "/icons/")):
        return ""
    if not (
        "tildacdn" in lower
        or re.search(r"\.(?:jpe?g|png|webp)(?:\?.*)?$", lower)
    ):
        return ""

    return result


def node_attr(node, names) -> str:
    for name in names:
        value = node.get(name)
        if value:
            return clean_text(value)
    return ""


def first_text(node, selectors) -> str:
    for selector in selectors:
        found = node.select_one(selector)
        if found:
            text = clean_text(found.get_text(" ", strip=True))
            if text:
                return text
    return ""


def product_link_from_card(card, page_url: str) -> str:
    direct = node_attr(
        card,
        (
            "data-product-url",
            "data-product-link",
            "data-product-href",
        ),
    )
    link = canonical_url(direct)
    if link:
        return link

    # Strongest link first: href inside the card pointing to /catalog/<slug>.
    for a in card.find_all("a", href=True):
        link = canonical_url(a.get("href"))
        if link:
            return link

    return ""


def product_title_from_card(card) -> str:
    title = node_attr(
        card,
        (
            "data-product-name",
            "data-product-title",
        ),
    )
    if not title:
        title = first_text(
            card,
            (
                ".js-store-prod-name",
                ".js-product-name",
                ".t-store__card__title",
                ".t-store__prod-popup__name",
                ".t-product__title",
                "h2",
                "h3",
            ),
        )
    return title[:150]


def product_price_from_card(card) -> str:
    for attr in (
        "data-product-price",
        "data-product-price-def",
        "data-product-price-value",
    ):
        value = card.get(attr)
        price = parse_number(value)
        if price:
            return price

    for selector in (
        ".js-product-price",
        ".js-store-prod-price",
        ".t-store__card__price-value",
        ".t-store__card__price",
        ".t-store__prod-popup__price-value",
        ".t-product__price",
    ):
        found = card.select_one(selector)
        if found:
            price = parse_number(found.get_text(" ", strip=True))
            if price:
                return price

    # Last fallback only inside the same card.
    text = clean_text(card.get_text(" ", strip=True))
    byn = re.search(
        r"(?<!\d)(\d{1,6}(?:[ \xa0]\d{3})*(?:[.,]\d{1,2})?)\s*BYN\b",
        text,
        flags=re.I,
    )
    if byn:
        return parse_number(byn.group(1))

    return ""


def product_image_from_card(card, page_url: str) -> str:
    # The key requirement: image must come from THIS card.
    for attr in (
        "data-product-img",
        "data-product-image",
        "data-product-photo",
    ):
        image = normalize_image(card.get(attr), page_url)
        if image:
            return image

    # In many Tilda stores the actual picture is nested in the card.
    for node in card.find_all(True):
        for attr in (
            "data-product-img",
            "data-original",
            "data-src",
            "data-img-zoom-url",
            "data-bg",
            "src",
        ):
            image = normalize_image(node.get(attr), page_url)
            if image:
                return image

        style = node.get("style") or ""
        for raw in re.findall(
            r"url\((?:['\"]?)([^)'\"\s]+)(?:['\"]?)\)",
            style,
            flags=re.I,
        ):
            image = normalize_image(raw, page_url)
            if image:
                return image

    return ""


def product_sku_from_card(card) -> str:
    sku = node_attr(
        card,
        (
            "data-product-sku",
            "data-product-code",
        ),
    )
    if sku:
        return sku

    text = clean_text(card.get_text(" ", strip=True))
    m = re.search(
        r"(?:Артикул|SKU)\s*:?\s*([A-Za-zА-Яа-яЁё0-9._/-]+)",
        text,
        flags=re.I,
    )
    return clean_text(m.group(1)) if m else ""


def product_description_from_card(card, title: str) -> str:
    value = node_attr(
        card,
        (
            "data-product-description",
            "data-product-descr",
        ),
    )
    if not value:
        value = first_text(
            card,
            (
                ".t-store__card__descr",
                ".js-store-prod-descr",
                ".t-store__prod-popup__descr",
                ".t-product__descr",
            ),
        )

    # Card descriptions are often absent. Use a factual generic sentence
    # rather than copying unrelated page text.
    if not value:
        value = f"{title}. Товар Banya Line для бани и сауны."

    return value[:5000]


def classify(title: str, url: str) -> str:
    haystack = f"{title} {url}".lower()
    for keys, category in TYPE_RULES:
        if any(key in haystack for key in keys):
            return category
    return "Баня и сауна > Аксессуары"


def extract_cards(soup: BeautifulSoup):
    """
    Return probable Tilda product card nodes. Selectors intentionally overlap;
    de-duplication by object id is done here and products by URL later.
    """
    selectors = [
        ".js-product",
        ".js-store-product",
        "[data-product-url]",
        "[data-product-lid]",
        "[data-product-gen-uid]",
        "[data-product-uid]",
    ]

    cards = []
    seen = set()
    for selector in selectors:
        for node in soup.select(selector):
            key = id(node)
            if key in seen:
                continue
            seen.add(key)
            cards.append(node)
    return cards


def parse_catalog_page(page_url: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = extract_cards(soup)
    print(f"{page_url}: probable product nodes = {len(cards)}")

    products = []

    for card in cards:
        link = product_link_from_card(card, page_url)
        if not link:
            continue

        title = product_title_from_card(card)
        price = product_price_from_card(card)
        image = product_image_from_card(card, page_url)

        # A valid Merchant product must have all four.
        if not title or not price or not image:
            continue

        sku = product_sku_from_card(card)
        pid = (
            re.sub(r"[^A-Za-z0-9._-]+", "-", sku).strip("-")[:50]
            if sku
            else urlparse(link).path.rstrip("/").split("/")[-1][:50]
        )

        products.append(
            {
                "id": pid,
                "title": title,
                "description": product_description_from_card(card, title),
                "link": link,
                "image_link": image,
                "availability": "in_stock",
                "price": f"{price} {CURRENCY}",
                "brand": BRAND,
                "condition": "new",
                "product_type": classify(title, link),
                "identifier_exists": "no",
            }
        )

    return products


def add_g(item, name: str, value: str):
    child = etree.SubElement(item, f"{{{G_NS}}}{name}")
    child.text = value


def build_xml(products: list[dict]):
    rss = etree.Element(
        "rss",
        version="2.0",
        nsmap={"g": G_NS},
    )
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = "Banya Line"
    etree.SubElement(channel, "link").text = BASE_URL
    etree.SubElement(channel, "description").text = (
        "Google Merchant Center product feed for Banya Line"
    )
    etree.SubElement(channel, "lastBuildDate").text = (
        datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    )

    for product in sorted(products, key=lambda x: x["id"]):
        item = etree.SubElement(channel, "item")
        for field in (
            "id",
            "title",
            "description",
            "link",
            "image_link",
            "availability",
            "price",
            "brand",
            "condition",
            "product_type",
            "identifier_exists",
        ):
            add_g(item, field, product[field])

    etree.ElementTree(rss).write(
        OUTPUT_FILE,
        encoding="utf-8",
        xml_declaration=True,
        pretty_print=True,
    )


def validate(products: list[dict]):
    if len(products) < 10:
        raise RuntimeError(
            f"Only {len(products)} valid products were extracted. "
            "Refusing to replace the existing feed."
        )

    images = {}
    for p in products:
        images.setdefault(p["image_link"], []).append(p["link"])

    if any(image_filename(x) in BAD_IMAGE_FILENAMES for x in images):
        raise RuntimeError("Known generic site image leaked into the feed.")

    biggest = max(len(urls) for urls in images.values())
    if biggest > max(6, int(len(products) * 0.20)):
        raise RuntimeError(
            f"Image validation failed: one image is used by "
            f"{biggest}/{len(products)} products."
        )

    # Variants can legitimately share photos, therefore no 1:1 requirement.
    if len(images) < max(8, len(products) // 5):
        raise RuntimeError(
            f"Image diversity too low: {len(images)} unique images "
            f"for {len(products)} products."
        )

    print(
        f"Validation OK: {len(products)} products, "
        f"{len(images)} unique images."
    )


def main():
    all_products = {}

    for index, path in enumerate(CATALOG_PAGES):
        page_url = urljoin(BASE_URL, path)

        try:
            html = fetch(page_url)
        except Exception as exc:
            print(f"[WARN] {exc}", file=sys.stderr)
            # Main catalog is important. If GitHub is blocked, stop quickly.
            if index == 0:
                raise RuntimeError(
                    "banyaline.by blocked the GitHub runner. "
                    "Retry the workflow later; the old merchant.xml was not changed."
                )
            continue

        found = parse_catalog_page(page_url, html)
        for product in found:
            # URL is a stable unique key and also prevents duplicate category cards.
            all_products.setdefault(product["link"], product)

        print(f"Unique valid products so far: {len(all_products)}")

        # If the main catalog already exposed most/all goods, avoid more requests.
        if index == 0 and len(all_products) >= 50:
            print("Main catalog contains enough products; section crawling skipped.")
            break

        # Gentle pause between section pages.
        time.sleep(1.5 + random.random())

    products = list(all_products.values())
    validate(products)
    build_xml(products)

    print(f"Done. Products in feed: {len(products)}")
    print(f"Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
