from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from catalogpulse.parse import Item

_ORDER = {"up": 0, "down": 1, "stock": 2, "missing": 3, "new": 4}
_BASE = [
    "title",
    "price",
    "currency",
    "stock",
    "price_raw",
    "url",
    "sku",
    "category",
    "image",
    "key",
]
_CHANGE_COLS = ["kind", "title", "old_price", "new_price", "pct", "old_stock", "new_stock", "url"]
_MONEY = {"price", "old_price", "new_price"}
# openpyxl refuses these. A stray vertical tab in a title took down a whole export once.
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    site TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages INTEGER NOT NULL DEFAULT 0,
    items INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    elapsed_ms INTEGER,
    threshold REAL NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    site TEXT NOT NULL,
    item_key TEXT NOT NULL,
    url TEXT,
    title TEXT,
    sku TEXT,
    category TEXT,
    image TEXT,
    currency TEXT,
    price REAL,
    price_text TEXT,
    in_stock INTEGER,
    stock_text TEXT,
    extra TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_run ON observations(run_id);
CREATE INDEX IF NOT EXISTS idx_obs_site_key ON observations(site, item_key);
"""


@dataclass(slots=True)
class Snap:
    key: str
    title: str | None
    url: str | None
    price: float | None
    in_stock: int | None


@dataclass(slots=True)
class Change:
    kind: str
    key: str
    title: str | None
    url: str | None
    old_price: float | None = None
    new_price: float | None = None
    pct: float | None = None
    old_stock: int | None = None
    new_stock: int | None = None

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "url": self.url,
            "old_price": self.old_price,
            "new_price": self.new_price,
            "pct": None if self.pct is None else round(self.pct, 2),
            "old_stock": None if self.old_stock is None else bool(self.old_stock),
            "new_stock": None if self.new_stock is None else bool(self.new_stock),
        }


def snap_of(item: Item) -> Snap:
    return Snap(item.key, item.title, item.url, item.price, item.in_stock)


def diff(previous: dict[str, Snap], current: dict[str, Snap], threshold: float, track_missing: bool) -> list[Change]:
    changes: list[Change] = []
    for key, row in current.items():
        old = previous.get(key)
        if old is None:
            changes.append(Change("new", key, row.title, row.url, new_price=row.price, new_stock=row.in_stock))
            continue
        old_cents, new_cents = _cents(old.price), _cents(row.price)
        if old_cents != new_cents and _worth(old.price, row.price, threshold):
            changes.append(
                Change(
                    _direction(old_cents, new_cents),
                    key,
                    row.title or old.title,
                    row.url or old.url,
                    old_price=old.price,
                    new_price=row.price,
                    pct=_pct(old.price, row.price),
                    old_stock=old.in_stock,
                    new_stock=row.in_stock,
                )
            )
        if old.in_stock is not None and row.in_stock is not None and old.in_stock != row.in_stock:
            changes.append(
                Change(
                    "stock",
                    key,
                    row.title or old.title,
                    row.url or old.url,
                    old_price=old.price,
                    new_price=row.price,
                    old_stock=old.in_stock,
                    new_stock=row.in_stock,
                )
            )
    if track_missing:
        for key, old in previous.items():
            if key in current:
                continue
            changes.append(Change("missing", key, old.title, old.url, old_price=old.price, old_stock=old.in_stock))
    changes.sort(key=lambda change: (_ORDER.get(change.kind, 9), (change.title or change.key).casefold()))
    return changes


def render(name, run_id, pages, items, elapsed_s, failed, complete, changes, baseline) -> str:
    lines = [f"{name}  run {run_id}  {pages} pages  {items} items  {elapsed_s:.1f}s"]
    if failed:
        lines.append(f"  failed {failed} page(s)")
    if not complete:
        lines.append("  partial crawl, unseen items are not marked missing")
    counts = {kind: 0 for kind in _ORDER}
    for change in changes:
        counts[change.kind] = counts.get(change.kind, 0) + 1
    if baseline:
        lines.append(f"  baseline, {counts.get('new', 0)} new")
        return "\n".join(lines)
    if not changes:
        lines.append("  no movement")
        return "\n".join(lines)
    lines.append("  new {new}  up {up}  down {down}  stock {stock}  missing {missing}".format(**counts))
    details = list(changes)
    if counts.get("new", 0) > 8:
        details = [change for change in changes if change.kind != "new"]
    shown = details[:20]
    for change in shown:
        lines.append("  " + _format_change(change))
    hidden = len(details) - len(shown)
    if hidden > 0:
        lines.append(f"  ... {hidden} more in the xlsx")
    elif counts.get("new", 0) > 8:
        lines.append(f"  {counts['new']} new rows are in the export")
    return "\n".join(lines)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def commit_run(
        self,
        site: str,
        items: list[Item],
        *,
        started_at: str,
        pages: int,
        failed: int,
        elapsed_ms: int,
        threshold: float,
        complete: bool,
    ) -> int:
        try:
            cur = self.conn.execute(
                """
                INSERT INTO runs (
                    site, started_at, finished_at, pages, items, failed,
                    elapsed_ms, threshold, complete
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    site,
                    started_at,
                    utcnow(),
                    pages,
                    len(items),
                    failed,
                    elapsed_ms,
                    threshold,
                    1 if complete else 0,
                ),
            )
            run_id = int(cur.lastrowid)
            self.conn.executemany(
                """
                INSERT INTO observations (
                    run_id, site, item_key, url, title, sku, category, image,
                    currency, price, price_text, in_stock, stock_text, extra
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [_obs_row(run_id, site, item) for item in items],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return run_id

    def run_index(self, site: str, run_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM runs
            WHERE site = ? AND finished_at IS NOT NULL AND id <= ?
            """,
            (site, run_id),
        ).fetchone()
        return int(row["n"])

    def history(self, site: str):
        run = self._run(site, before=None)
        if run is None:
            return None, [], {}
        items = self.items_for(int(run["id"]))
        prior = self._run(site, before=int(run["id"]))
        previous = {}
        if prior is not None:
            for item in self.items_for(int(prior["id"])):
                previous[item.key] = snap_of(item)
        return run, items, previous

    def items_for(self, run_id: int) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [_item_from_row(row) for row in rows]

    def _run(self, site: str, before: int | None):
        if before is None:
            return self.conn.execute(
                """
                SELECT * FROM runs
                WHERE site = ? AND finished_at IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (site,),
            ).fetchone()
        return self.conn.execute(
            """
            SELECT * FROM runs
            WHERE site = ? AND finished_at IS NOT NULL AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (site, before),
        ).fetchone()


def write_outputs(directory: Path, meta: dict, items: list[Item], changes: list[Change], formats: set[str]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    ordered = sorted(items, key=lambda item: (item.title or item.key).casefold())
    written: list[Path] = []
    if "json" in formats:
        path = directory / "latest.json"
        payload = {
            "site": meta["site"],
            "run_id": meta["run_id"],
            "started_at": meta["started_at"],
            "pages": meta["pages"],
            "failed": meta["failed"],
            "complete": meta["complete"],
            "items": [item.as_dict() for item in ordered],
            "changes": [change.as_dict() for change in changes],
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(path)
    if "csv" in formats:
        path = directory / "latest.csv"
        _write_csv(path, ordered)
        written.append(path)
    if "xlsx" in formats:
        path = directory / "latest.xlsx"
        _write_xlsx(path, ordered, changes)
        written.append(path)
    return written


def _worth(old, new, threshold: float) -> bool:
    if threshold <= 0:
        return True
    pct = _pct(old, new)
    if pct is None:
        return True
    return abs(pct) + 1e-9 >= threshold


def _direction(old_cents, new_cents) -> str:
    if old_cents is None:
        return "up"
    if new_cents is None or new_cents < old_cents:
        return "down"
    return "up"


def _pct(old, new):
    if old is None or new is None or abs(old) < 1e-9:
        return None
    return (new - old) / abs(old) * 100


def _cents(value):
    if value is None:
        return None
    return int(round(value * 100))


def _format_change(change: Change) -> str:
    title = (change.title or change.key).replace("\n", " ")
    if len(title) > 64:
        title = title[:61] + "..."
    if change.kind in {"up", "down"}:
        pct = "" if change.pct is None else f" ({change.pct:+.1f}%)"
        return f"{change.kind:<7} {title}  {_money(change.old_price)} -> {_money(change.new_price)}{pct}"
    if change.kind == "stock":
        return f"stock   {title}  {_yn(change.old_stock)} -> {_yn(change.new_stock)}"
    if change.kind == "missing":
        return f"missing {title}  was {_money(change.old_price)}"
    return f"new     {title}  {_money(change.new_price)}"


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _yn(value) -> str:
    if value is None:
        return "-"
    return "in" if value else "out"


def _obs_row(run_id: int, site: str, item: Item) -> tuple:
    extra = json.dumps(item.extra, ensure_ascii=False, sort_keys=True) if item.extra else None
    return (
        run_id,
        site,
        item.key,
        item.url,
        item.title,
        item.sku,
        item.category,
        item.image,
        item.currency,
        item.price,
        item.price_text,
        item.in_stock,
        item.stock_text,
        extra,
    )


def _item_from_row(row: sqlite3.Row) -> Item:
    extra = {}
    if row["extra"]:
        try:
            loaded = json.loads(row["extra"])
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            extra = {str(key): str(value) for key, value in loaded.items()}
    return Item(
        key=row["item_key"],
        url=row["url"],
        title=row["title"],
        sku=row["sku"],
        category=row["category"],
        image=row["image"],
        currency=row["currency"],
        price=row["price"],
        price_text=row["price_text"],
        in_stock=row["in_stock"],
        stock_text=row["stock_text"],
        extra=extra,
    )


def _write_csv(path: Path, items: list[Item]) -> None:
    headers, rows = _catalog_table(items)
    # Excel on Windows still guesses the code page unless the BOM is there.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)


def _write_xlsx(path: Path, items: list[Item], changes: list[Change]) -> None:
    book = Workbook()
    catalog = book.active
    catalog.title = "Catalog"
    headers, rows = _catalog_table(items)
    _fill(catalog, headers, rows)
    sheet = book.create_sheet("Changes")
    _fill(sheet, _CHANGE_COLS, [_change_row(change) for change in changes])
    book.save(path)


def _catalog_table(items: list[Item]):
    extra_keys = []
    seen = set()
    for item in items:
        for key in item.extra:
            if key not in seen:
                seen.add(key)
                extra_keys.append(key)
    extra_keys.sort()
    headers = _BASE + extra_keys
    rows = []
    for item in items:
        base = {
            "title": item.title or "",
            "price": item.price,
            "currency": item.currency or "",
            "stock": _stock_label(item),
            "price_raw": item.price_text or "",
            "url": item.url or "",
            "sku": item.sku or "",
            "category": item.category or "",
            "image": item.image or "",
            "key": item.key,
        }
        rows.append([_cell(base.get(column, item.extra.get(column, ""))) for column in headers])
    return headers, rows


def _change_row(change: Change) -> list:
    return [
        change.kind,
        _cell(change.title or ""),
        change.old_price,
        change.new_price,
        None if change.pct is None else round(change.pct, 2),
        _stock_label_bit(change.old_stock),
        _stock_label_bit(change.new_stock),
        _cell(change.url or ""),
    ]


def _cell(value):
    if isinstance(value, str):
        return _ILLEGAL.sub("", value)
    return value


def _fill(sheet, headers, rows) -> None:
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    bold = Font(bold=True)
    for cell in sheet[1]:
        cell.font = bold
    sheet.freeze_panes = "A2"
    if headers:
        sheet.auto_filter.ref = sheet.dimensions
    for index, header in enumerate(headers, start=1):
        if header in _MONEY:
            fmt = "#,##0.00"
        elif header == "pct":
            fmt = "0.00"
        else:
            fmt = None
        if fmt:
            for cell in sheet.iter_cols(min_col=index, max_col=index, min_row=2):
                for one in cell:
                    if isinstance(one.value, (int, float)):
                        one.number_format = fmt
        width = len(header)
        for cell in sheet.iter_cols(min_col=index, max_col=index, max_row=80):
            for one in cell:
                width = max(width, len(str(one.value or "")))
        sheet.column_dimensions[get_column_letter(index)].width = min(max(width + 2, 10), 56)


def _stock_label(item: Item) -> str:
    if item.stock_text:
        return item.stock_text
    return _stock_label_bit(item.in_stock)


def _stock_label_bit(value) -> str:
    if value is None:
        return ""
    return "in stock" if value else "out of stock"
