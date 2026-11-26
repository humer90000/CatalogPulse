from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selectolax.lexbor import LexborHTMLParser

from catalogpulse.config import FieldSpec, Site

# Last separator wins when both comma and dot are present (1.299,50 vs 1,299.50).
# A bare 1.299 / 1,299 with groups of three is thousands, not a decimal.
_THOUSANDS_COMMA = re.compile(r"\d{1,3}(,\d{3})+")
_THOUSANDS_DOT = re.compile(r"\d{1,3}(\.\d{3})+")
_NUMBER = re.compile(r"\d[\d\s\u00a0.,]*")
_SALE = re.compile(r"\b(now|sale|сейчас|акция)\b", re.I)
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_ISO = re.compile(r"\b(USD|EUR|GBP|RUB|JPY|CHF|CAD|AUD|UAH|PLN)\b", re.I)
_PAGE_OF = re.compile(r"page\s+\d+\s+of\s+(\d+)", re.I)
_LDJSON = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.I | re.S,
)

_SYMBOLS = {
    "£": "GBP",
    "$": "USD",
    "€": "EUR",
    "¥": "JPY",
    "₽": "RUB",
    "₴": "UAH",
}

_OUT = (
    "out of stock",
    "sold out",
    "unavailable",
    "нет в наличии",
    "нет на складе",
)
_IN = (
    "in stock",
    "available",
    "в наличии",
    "есть в наличии",
)

_CORE = {"title", "url", "price", "stock", "sku", "image"}
_HARD_CAP = 500


@dataclass(slots=True)
class Item:
    key: str
    url: str | None
    title: str | None
    sku: str | None
    category: str | None
    image: str | None
    currency: str | None
    price: float | None
    price_text: str | None
    in_stock: int | None
    stock_text: str | None
    extra: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        stock = None if self.in_stock is None else bool(self.in_stock)
        return {
            "key": self.key,
            "title": self.title,
            "url": self.url,
            "price": self.price,
            "currency": self.currency,
            "price_raw": self.price_text,
            "in_stock": stock,
            "stock_raw": self.stock_text,
            "sku": self.sku,
            "category": self.category,
            "image": self.image,
            "extra": self.extra or {},
        }


def split_price(raw: str | None) -> tuple[float | None, str | None]:
    if not raw:
        return None, None
    text = " ".join(str(raw).replace("\u00a0", " ").split())
    if not text:
        return None, None
    currency = None
    for symbol, code in _SYMBOLS.items():
        if symbol in text:
            currency = code
            break
    if currency is None:
        iso = _ISO.search(text)
        if iso:
            currency = iso.group(1).upper()
    matches = list(_NUMBER.finditer(text))
    if not matches:
        return None, currency
    chosen = matches[0]
    for match in matches:
        prefix = text[max(0, match.start() - 16) : match.start()]
        if _SALE.search(prefix):
            chosen = match
            break
    try:
        return _to_float(chosen.group(0)), currency
    except ValueError:
        return None, currency


def _to_float(num: str) -> float:
    s = "".join(num.split()).strip(".")
    if not s:
        raise ValueError(num)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", "") if _THOUSANDS_COMMA.fullmatch(s) else s.replace(",", ".")
    elif "." in s and _THOUSANDS_DOT.fullmatch(s):
        s = s.replace(".", "")
    return float(s)


def stock_flag(raw: str | None) -> tuple[int | None, str | None]:
    if raw is None:
        return None, None
    text = " ".join(str(raw).split())
    if not text:
        return None, None
    low = text.lower()
    # "unavailable" contains "available", so the out-of-stock phrases go first.
    if any(piece in low for piece in _OUT):
        return 0, text
    if any(piece in low for piece in _IN):
        return 1, text
    return None, text


def canonical_listing(url: str) -> str:
    parts = urlsplit(url.strip())
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key in {"page", "p"} and value in {"", "1"}:
            continue
        query.append((key, value))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def read_page_total(html: str, selector: str) -> int | None:
    node = LexborHTMLParser(html).css_first(selector)
    if node is None:
        return None
    return _page_total(node.text(separator=" ", strip=True))


def _page_total(text: str) -> int | None:
    # "Showing 1 to 20 of 1000 results" is a row count, not a page count.
    match = _PAGE_OF.search(text)
    if not match:
        return None
    total = int(match.group(1))
    return total if total >= 1 else None


def expand_template(template: str, page_url: str, total: int, cap: int) -> tuple[list[str], bool]:
    limit = total if total <= cap else cap
    start = canonical_listing(page_url)
    urls = []
    # The start URL is already page 1. page-1.html is either a duplicate of
    # index.html or a 404, so the numbered template starts at 2.
    for number in range(2, limit + 1):
        raw = template.replace("{page}", str(number))
        url = raw if raw.startswith(("http://", "https://")) else urljoin(page_url, raw)
        if canonical_listing(url) == start:
            continue
        urls.append(url)
    return urls, limit < total


def linked_pages(html: str, page_url: str, selectors: list[str]) -> list[str]:
    if not selectors:
        return []
    tree = LexborHTMLParser(html)
    found: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        for node in tree.css(selector):
            href = (node.attributes or {}).get("href")
            if not href:
                continue
            href = href.strip()
            if not href or href.startswith("#") or href.lower().startswith(("javascript:", "mailto:")):
                continue
            absolute = urljoin(page_url, href)
            key = canonical_listing(absolute)
            if key in seen:
                continue
            seen.add(key)
            found.append(absolute)
    return found


def parse_listing(html: str, page_url: str, site: Site) -> list[Item]:
    tree = LexborHTMLParser(html)
    items: list[Item] = []
    for node in tree.css(site.item):
        raw: dict[str, str | None] = {}
        for name, spec in site.fields.items():
            raw[name] = _read(node, spec)
        item = _build(raw, page_url, site)
        if item is not None:
            items.append(item)
    if site.jsonld:
        items = _merge_jsonld(items, html, page_url, site)
    return items


def _read(node, spec: FieldSpec) -> str | None:
    found = node.css_first(spec.selector)
    if found is None:
        return None
    if spec.attr:
        value = (found.attributes or {}).get(spec.attr) or ""
    else:
        value = found.text(separator=" ", strip=True)
    value = _clean(value)
    if not value:
        return None
    if spec.map:
        mapped = _apply_map(value, spec.map)
        if mapped is not None:
            return mapped
    return value


def _apply_map(value: str, mapping: dict[str, str]) -> str | None:
    folded = {key.lower(): mapped for key, mapped in mapping.items()}
    for token in value.split():
        if token in mapping:
            return mapping[token]
        hit = folded.get(token.lower())
        if hit is not None:
            return hit
    return None


def _build(raw: dict[str, str | None], page_url: str, site: Site) -> Item | None:
    url = _abs(page_url, raw.get("url"))
    image = _abs(page_url, raw.get("image"))
    title = _clean(raw.get("title"))
    sku = _clean(raw.get("sku"))
    price_text = _clean(raw.get("price"))
    price, seen_currency = split_price(price_text)
    currency = seen_currency or site.currency
    in_stock, stock_text = stock_flag(raw.get("stock"))

    key = _identity(raw, page_url, site)
    if not key:
        return None

    extra: dict[str, str] = {}
    for name, value in raw.items():
        if name in _CORE or not value:
            continue
        extra[name] = value

    category = _clean(raw.get("category")) or site.category
    return Item(
        key=key,
        url=url,
        title=title,
        sku=sku,
        category=category,
        image=image,
        currency=currency,
        price=price,
        price_text=price_text,
        in_stock=in_stock,
        stock_text=stock_text,
        extra=extra,
    )


def _identity(raw: dict[str, str | None], page_url: str, site: Site) -> str | None:
    names = site.key if isinstance(site.key, tuple) else (site.key,)
    parts: list[str] = []
    for name in names:
        if name == "url":
            value = _abs(page_url, raw.get("url"))
        else:
            value = _clean(raw.get(name))
            if value and name == "image":
                value = _abs(page_url, value)
        if not value:
            return None
        parts.append(value)
    if len(parts) == 1:
        return parts[0]
    return " | ".join(parts)


def _abs(page_url: str, value: str | None) -> str | None:
    value = _clean(value)
    if not value:
        return None
    if value.startswith(("http://", "https://")):
        return value
    return urljoin(page_url, value)


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = _ILLEGAL.sub("", str(value))
    text = " ".join(text.split())
    return text or None


def _merge_jsonld(items: list[Item], html: str, page_url: str, site: Site) -> list[Item]:
    products = list(_jsonld_products(html))
    if not products:
        return items
    by_url, by_name = _jsonld_index(products, page_url)
    if not items:
        built = []
        for product in products:
            item = _item_from_jsonld(product, page_url, site)
            if item is not None:
                built.append(item)
        return built

    for item in items:
        product = None
        if item.url:
            product = by_url.get(canonical_listing(item.url))
        if product is None and item.title:
            product = by_name.get(item.title.casefold())
        if product is None:
            continue
        _fill_gaps(item, product, page_url, site)
    return items


def _jsonld_products(html: str):
    # selectolax rewrites script bodies. The JSON is easier to lift as text.
    seen: set[int] = set()
    for match in _LDJSON.finditer(html):
        blob = match.group(1).strip()
        if not blob:
            continue
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        yield from _walk(data, seen)


def _walk(node, seen: set[int]):
    if isinstance(node, list):
        for part in node:
            yield from _walk(part, seen)
        return
    if not isinstance(node, dict):
        return
    marker = id(node)
    if marker in seen:
        return
    seen.add(marker)
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    if any(name in {"Product", "ProductGroup"} for name in kinds if isinstance(name, str)):
        yield node
    for value in node.values():
        if isinstance(value, (dict, list)):
            yield from _walk(value, seen)


def _jsonld_index(products, page_url: str):
    by_url = {}
    by_name = {}
    for product in products:
        url = _abs(page_url, _as_text(product.get("url")))
        name = _clean(_as_text(product.get("name")))
        if url:
            by_url[canonical_listing(url)] = product
        if name:
            by_name.setdefault(name.casefold(), product)
    return by_url, by_name


def _fill_gaps(item: Item, product: dict, page_url: str, site: Site) -> None:
    offer = _offer(product)
    if item.price is None and offer.get("price") is not None:
        price, currency = _numberish(offer.get("price"))
        item.price = price
        item.price_text = _clean(_as_text(offer.get("price")))
        item.currency = item.currency or currency or _clean(offer.get("currency")) or site.currency
    if not item.sku:
        item.sku = _clean(_as_text(product.get("sku") or product.get("mpn")))
    if not item.image:
        item.image = _abs(page_url, _image(product))
    if item.in_stock is None:
        flag = _schema_stock(offer.get("availability"))
        if flag is not None:
            item.in_stock = flag
            item.stock_text = item.stock_text or _clean(_as_text(offer.get("availability")))
    if not item.title:
        item.title = _clean(_as_text(product.get("name")))


def _item_from_jsonld(product: dict, page_url: str, site: Site) -> Item | None:
    offer = _offer(product)
    url = _abs(page_url, _as_text(product.get("url")))
    title = _clean(_as_text(product.get("name")))
    sku = _clean(_as_text(product.get("sku") or product.get("mpn")))
    price, currency = _numberish(offer.get("price"))
    key = url or sku or title
    if not key:
        return None
    return Item(
        key=key,
        url=url,
        title=title,
        sku=sku,
        category=site.category,
        image=_abs(page_url, _image(product)),
        currency=currency or _clean(offer.get("currency")) or site.currency,
        price=price,
        price_text=_clean(_as_text(offer.get("price"))),
        in_stock=_schema_stock(offer.get("availability")),
        stock_text=_clean(_as_text(offer.get("availability"))),
    )


def _offer(node: dict) -> dict:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return {}
    return {
        "price": offers.get("price", offers.get("lowPrice")),
        "currency": offers.get("priceCurrency"),
        "availability": offers.get("availability"),
    }


def _image(node: dict) -> str | None:
    image = node.get("image")
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        return _as_text(image.get("url") or image.get("contentUrl"))
    return _as_text(image)


def _numberish(value) -> tuple[float | None, str | None]:
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        return split_price(value)
    return None, None


def _schema_stock(value) -> int | None:
    text = _as_text(value)
    if not text:
        return None
    low = text.lower()
    if any(piece in low for piece in ("outofstock", "soldout", "discontinued")):
        return 0
    if "instock" in low:
        return 1
    return None


def _as_text(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def pager_selectors(site: Site) -> list[str]:
    selectors = []
    if site.pager.page_links:
        selectors.append(site.pager.page_links)
    if site.pager.next:
        selectors.append(site.pager.next)
    return selectors


def page_cap(site: Site) -> int:
    return site.pager.max_pages or _HARD_CAP
