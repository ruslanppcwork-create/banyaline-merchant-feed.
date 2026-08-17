#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations
import json, math, re, sys, time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from lxml import etree

BASE_URL = "https://banyaline.by"
API_URL = "https://store.tildaapi.biz/api/getproductslist/"
STORE_PART_UID = "939321134672"
REC_ID = "1405730431"
PAGE_SIZE = 9
OUTPUT_FILE = "merchant.xml"
G_NS = "http://base.google.com/ns/1.0"
BRAND = "Banya Line"
CURRENCY = "BYN"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
    "Referer": "https://banyaline.by/catalog",
    "Origin": "https://banyaline.by",
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
    (("перенос", "дров"), "Баня и сауна > Аксессуары > Дровницы"),
    (("вешал", "полк"), "Баня и сауна > Вешалки и полки"),
    (("запарк",), "Баня и сауна > Запарки"),
    (("комплект", "набор"), "Баня и сауна > Наборы"),
]

META_KEYS = {"uid", "externalid", "sku", "price", "priceold", "quantity", "img"}
session = requests.Session()
session.headers.update(HEADERS)

def clean(v):
    return re.sub(r"\s+", " ", str(v)).strip() if v is not None else ""

def fetch_slice(slice_no):
    params = {
        "storepartuid": STORE_PART_UID,
        "recid": REC_ID,
        "c": str(int(time.time() * 1000)),
        "getparts": "true",
        "getoptions": "true",
        "slice": str(slice_no),
        "size": str(PAGE_SIZE),
        "flag_root": "withroot",
    }
    last = None
    for attempt, delay in enumerate((0, 5, 15), 1):
        if delay:
            time.sleep(delay)
        try:
            r = session.get(API_URL, params=params, timeout=30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            data = json.loads(r.text)
            if not isinstance(data, dict) or "products" not in data:
                raise RuntimeError("Unexpected Tilda API response")
            return data
        except Exception as e:
            last = e
            print(f"[WARN] slice={slice_no}, attempt={attempt}: {e}", file=sys.stderr)
    raise RuntimeError(f"Cannot fetch slice {slice_no}: {last}")

def fetch_all_products():
    first = fetch_slice(1)
    total = int(first.get("total") or 0)
    products = list(first.get("products") or [])
    print(f"Tilda total: {total}; slice 1: {len(products)}")
    if total <= 0:
        raise RuntimeError("Tilda returned total=0")
    next_slice = first.get("nextslice")
    seen = {1}
    while next_slice:
        s = int(next_slice)
        if s in seen:
            break
        seen.add(s)
        data = fetch_slice(s)
        chunk = list(data.get("products") or [])
        products.extend(chunk)
        print(f"Slice {s}: {len(chunk)}")
        next_slice = data.get("nextslice")
        if len(seen) > max(20, math.ceil(total / PAGE_SIZE) + 3):
            raise RuntimeError("Pagination loop")
    unique = {}
    for p in products:
        uid = clean(p.get("uid"))
        if uid:
            unique[uid] = p
    result = list(unique.values())
    if len(result) < min(total, 10):
        raise RuntimeError(f"Fetched only {len(result)} of {total}")
    return result

def parse_gallery(product):
    raw = product.get("gallery")
    if not raw:
        return []
    if isinstance(raw, list):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception:
            return []
    return [clean(x.get("img")) for x in data if isinstance(x, dict) and clean(x.get("img"))]

def catalog_path_from_option(value):
    value = clean(value)
    if not value:
        return "", ""
    if ":/catalog/" in value:
        label, path = value.split(":", 1)
        return label.strip(), path.strip()
    if "/catalog/" in value:
        idx = value.find("/catalog/")
        return value[:idx].rstrip(": ").strip(), value[idx:].strip()
    return value, ""

def variant_route(edition):
    path = color = size = ""
    for key, raw in edition.items():
        if key in META_KEYS:
            continue
        value = clean(raw)
        if not value:
            continue
        label, candidate_path = catalog_path_from_option(value)
        if candidate_path and not path:
            path = candidate_path
        kl = str(key).lower()
        if "цвет" in kl and not color:
            color = label or value
        elif "размер" in kl and not size:
            size = label or value
    return path, color, size

def product_type(title):
    low = title.lower()
    for keys, cat in TYPE_RULES:
        if any(k in low for k in keys):
            return cat
    return "Баня и сауна > Аксессуары"

def generic_description(title, ptype):
    if "Матрасы" in ptype:
        return f"{title} Banya Line для отдыха и парения в бане и сауне. Банный аксессуар с натуральным наполнителем."
    if "Валики" in ptype:
        return f"{title} Banya Line для комфортного положения тела во время отдыха и парения в бане и сауне."
    if "Подушки" in ptype:
        return f"{title} Banya Line для отдыха в бане и сауне."
    if "Вееры" in ptype:
        return f"{title} Banya Line для разгона пара и работы с воздушным потоком."
    if "Килты" in ptype:
        return f"{title} Banya Line для комфортного использования в бане и сауне."
    if "Дровницы" in ptype:
        return f"{title} Banya Line для переноски и хранения дров."
    return f"{title} Banya Line — товар для бани и сауны."

def availability(quantity):
    q = clean(quantity)
    if q == "":
        return "in_stock"
    try:
        return "in_stock" if float(q.replace(",", ".")) > 0 else "out_of_stock"
    except Exception:
        return "in_stock"

def stable_offer_id(parent_uid, link):
    slug = urlparse(link).path.rstrip("/").split("/")[-1]
    return f"{parent_uid}-{slug}"[:50]

def variant_label(edition):
    labels = []
    for key, raw in edition.items():
        if key in META_KEYS:
            continue
        value = clean(raw)
        if not value:
            continue
        label, _ = catalog_path_from_option(value)
        display = label or value
        if display and display not in labels:
            labels.append(display)
    return labels

def offers_from_product(product):
    parent_uid = clean(product.get("uid"))
    parent_title = clean(product.get("title"))
    parent_descr = clean(product.get("descr"))
    parent_url = clean(product.get("url") or product.get("buttonlink"))
    parent_price = clean(product.get("price"))
    parent_qty = product.get("quantity")
    gallery = parse_gallery(product)
    editions = product.get("editions") if isinstance(product.get("editions"), list) else []
    ptype = product_type(parent_title)

    by_link = {}
    for edition in editions:
        path, color, size = variant_route(edition)
        link = urljoin(BASE_URL, path) if path else parent_url
        if link and link not in by_link:
            by_link[link] = {"edition": edition, "color": color, "size": size}

    offers = []
    if by_link:
        for link, data in by_link.items():
            edition = data["edition"]
            price = clean(edition.get("price") or parent_price)
            image = clean(edition.get("img") or (gallery[0] if gallery else ""))
            if not price or not image:
                continue
            labels = variant_label(edition)
            title_parts = [parent_title]
            for label in labels:
                if label and label.lower() not in parent_title.lower():
                    title_parts.append(label)
            offers.append({
                "id": stable_offer_id(parent_uid, link),
                "item_group_id": parent_uid,
                "title": ", ".join(title_parts)[:150],
                "description": parent_descr[:5000] if parent_descr else generic_description(parent_title, ptype),
                "link": link,
                "image_link": image,
                "availability": availability(edition.get("quantity")),
                "price": f"{float(price):.2f} {CURRENCY}",
                "condition": "new",
                "brand": BRAND,
                "identifier_exists": "no",
                "product_type": ptype,
                "color": data["color"],
                "size": data["size"],
            })
    else:
        image = gallery[0] if gallery else ""
        if parent_url and parent_price and image:
            offers.append({
                "id": stable_offer_id(parent_uid, parent_url),
                "item_group_id": parent_uid,
                "title": parent_title[:150],
                "description": parent_descr[:5000] if parent_descr else generic_description(parent_title, ptype),
                "link": parent_url,
                "image_link": image,
                "availability": availability(parent_qty),
                "price": f"{float(parent_price):.2f} {CURRENCY}",
                "condition": "new",
                "brand": BRAND,
                "identifier_exists": "no",
                "product_type": ptype,
                "color": "",
                "size": "",
            })
    return offers

def add_g(item, name, value):
    if value in (None, ""):
        return
    n = etree.SubElement(item, f"{{{G_NS}}}{name}")
    n.text = str(value)

def validate_offers(offers):
    if len(offers) < 20:
        raise RuntimeError(f"Only {len(offers)} offers generated")
    images = [x["image_link"] for x in offers]
    if any("Frame_755829" in img for img in images):
        raise RuntimeError("Generic image leaked into feed")
    if len(set(images)) < 10:
        raise RuntimeError(f"Only {len(set(images))} unique images for {len(offers)} offers")
    print(f"Validation OK: {len(offers)} offers, {len(set(images))} unique images")

def build_feed(offers):
    rss = etree.Element("rss", version="2.0", nsmap={"g": G_NS})
    ch = etree.SubElement(rss, "channel")
    etree.SubElement(ch, "title").text = "Banya Line"
    etree.SubElement(ch, "link").text = BASE_URL
    etree.SubElement(ch, "description").text = "Banya Line Google Merchant Center feed from Tilda Store API"
    etree.SubElement(ch, "lastBuildDate").text = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    for offer in sorted(offers, key=lambda x: x["id"]):
        item = etree.SubElement(ch, "item")
        for field in ("id","item_group_id","title","description","link","image_link","availability","price","condition","brand","identifier_exists","product_type","color","size"):
            add_g(item, field, offer.get(field, ""))
    etree.ElementTree(rss).write(OUTPUT_FILE, encoding="utf-8", xml_declaration=True, pretty_print=True)

def main():
    products = fetch_all_products()
    offers = []
    for p in products:
        generated = offers_from_product(p)
        offers.extend(generated)
        print(f"{clean(p.get('title'))}: {len(generated)} offer(s)")
    unique = {o["id"]: o for o in offers}
    offers = list(unique.values())
    validate_offers(offers)
    build_feed(offers)
    print(f"Done. Merchant offers: {len(offers)}")
    print(f"Output: {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
