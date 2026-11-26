from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


class ConfigError(Exception):
    pass


@dataclass(slots=True)
class FieldSpec:
    selector: str
    attr: str | None = None
    map: dict[str, str] | None = None


@dataclass(slots=True)
class Pager:
    next: str | None = None
    page_count: str | None = None
    url_template: str | None = None
    page_links: str | None = None
    max_pages: int | None = None


@dataclass(slots=True)
class Site:
    name: str
    start_url: str
    item: str
    fields: dict[str, FieldSpec]
    key: str | tuple[str, ...] = "url"
    category: str | None = None
    currency: str | None = None
    price_change_pct: float = 0.0
    concurrency: int = 8
    timeout: float = 20.0
    retries: int = 3
    delay: tuple[float, float] = (0.0, 0.0)
    jsonld: bool = True
    obey_robots: bool = True
    headers: dict[str, str] = field(default_factory=dict)
    pager: Pager = field(default_factory=Pager)
    source: str = ""


def load_site(path: Path) -> Site:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(str(exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path.name}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: the file should be a mapping")

    name = str(_need(raw, "name", path.name))
    if not _SLUG.fullmatch(name):
        raise ConfigError(f"{path.name}: name {name!r} is not a slug")

    start_url = str(_need(raw, "start_url", path.name)).strip()
    if not start_url.startswith(("http://", "https://")):
        raise ConfigError(f"{path.name}: start_url must be http or https")

    fields_raw = raw.get("fields")
    if not isinstance(fields_raw, dict) or not fields_raw:
        raise ConfigError(f"{path.name}: fields must list at least one selector")
    fields = {str(key): _field(value, path.name, str(key)) for key, value in fields_raw.items()}

    key = _key(raw.get("key"), fields, path.name)

    concurrency = _int(raw.get("concurrency", 8), path.name, "concurrency")
    if not 1 <= concurrency <= 40:
        raise ConfigError(f"{path.name}: concurrency must be between 1 and 40")
    retries = _int(raw.get("retries", 3), path.name, "retries")
    if not 1 <= retries <= 8:
        raise ConfigError(f"{path.name}: retries must be between 1 and 8")
    timeout = float(raw.get("timeout", 20))
    if not 1 <= timeout <= 180:
        raise ConfigError(f"{path.name}: timeout must be between 1 and 180")
    threshold = float(raw.get("price_change_pct", 0))
    if threshold < 0:
        raise ConfigError(f"{path.name}: price_change_pct can't be negative")

    headers_raw = raw.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ConfigError(f"{path.name}: headers must be a mapping")
    headers = {str(k): str(v) for k, v in headers_raw.items()}

    category = raw.get("category")
    currency = raw.get("currency")
    return Site(
        name=name,
        start_url=start_url,
        item=str(_need(raw, "item", path.name)),
        fields=fields,
        key=key,
        category=None if category in (None, "") else str(category),
        currency=None if currency in (None, "") else str(currency).upper(),
        price_change_pct=threshold,
        concurrency=concurrency,
        timeout=timeout,
        retries=retries,
        delay=_delay(raw.get("delay"), path.name),
        jsonld=bool(raw.get("jsonld", True)),
        obey_robots=bool(raw.get("obey_robots", True)),
        headers=headers,
        pager=_pager(raw.get("pagination"), path.name),
        source=str(path),
    )


def _key(value, fields: dict, name: str) -> str | tuple[str, ...]:
    if isinstance(value, list):
        parts = tuple(str(part) for part in value)
        if not parts:
            raise ConfigError(f"{name}: key is empty")
        missing = [part for part in parts if part not in fields]
        if missing:
            raise ConfigError(f"{name}: key uses unknown fields: {', '.join(missing)}")
        return parts
    key = str(value or "url")
    if key != "url" and key not in fields:
        raise ConfigError(f"{name}: key {key!r} is not a field")
    if key == "url" and "url" not in fields:
        raise ConfigError(f"{name}: add a url field, or set key to another field")
    return key


def _need(raw: dict, key: str, name: str):
    value = raw.get(key)
    if value in (None, ""):
        raise ConfigError(f"{name}: missing {key}")
    return value


def _int(value, name: str, label: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name}: {label} must be an integer") from exc


def _delay(raw, name: str) -> tuple[float, float]:
    if raw is None:
        return (0.0, 0.0)
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value < 0:
            raise ConfigError(f"{name}: delay can't be negative")
        return (value, value)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        lo, hi = float(raw[0]), float(raw[1])
        if lo > hi:
            lo, hi = hi, lo
        if lo < 0:
            raise ConfigError(f"{name}: delay can't be negative")
        return (lo, hi)
    raise ConfigError(f"{name}: delay is a number or [min, max]")


def _field(raw, name: str, key: str) -> FieldSpec:
    if isinstance(raw, str):
        if not raw.strip():
            raise ConfigError(f"{name}: field {key} has an empty selector")
        return FieldSpec(selector=raw.strip())
    if not isinstance(raw, dict) or not raw.get("selector"):
        raise ConfigError(f"{name}: field {key} needs a selector")
    mapping = raw.get("map")
    if mapping is not None:
        if not isinstance(mapping, dict):
            raise ConfigError(f"{name}: field {key} map must be a mapping")
        mapping = {str(k): str(v) for k, v in mapping.items()}
    attr = raw.get("attr")
    return FieldSpec(
        selector=str(raw["selector"]).strip(),
        attr=None if attr in (None, "") else str(attr),
        map=mapping,
    )


def _pager(raw, name: str) -> Pager:
    if raw is None:
        return Pager()
    if not isinstance(raw, dict):
        raise ConfigError(f"{name}: pagination must be a mapping")
    max_pages = raw.get("max_pages")
    if max_pages in (None, ""):
        limit = None
    else:
        limit = _int(max_pages, name, "max_pages")
        if limit < 1:
            raise ConfigError(f"{name}: max_pages must be >= 1")
    return Pager(
        next=_blank(raw.get("next")),
        page_count=_blank(raw.get("page_count")),
        url_template=_blank(raw.get("url_template")),
        page_links=_blank(raw.get("page_links")),
        max_pages=limit,
    )


def _blank(value) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
