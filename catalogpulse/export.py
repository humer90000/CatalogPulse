from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from catalogpulse.changes import Change
from catalogpulse.parse import Item

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
_CHANGE = ["kind", "title", "old_price", "new_price", "pct", "old_stock", "new_stock", "url"]
_MONEY = {"price", "old_price", "new_price"}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def write_outputs(
    directory: Path,
    meta: dict,
    items: list[Item],
    changes: list[Change],
    formats: set[str],
) -> list[Path]:
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
    _fill(sheet, _CHANGE, [_change_row(change) for change in changes])
    book.save(path)


def _catalog_table(items: list[Item]) -> tuple[list[str], list[list]]:
    extra_keys: list[str] = []
    seen: set[str] = set()
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
        _stock_bit(change.old_stock),
        _stock_bit(change.new_stock),
        _cell(change.url or ""),
    ]


def _cell(value):
    if isinstance(value, str):
        return _ILLEGAL.sub("", value)
    return value


def _fill(sheet, headers: list[str], rows: list[list]) -> None:
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
    return _stock_bit(item.in_stock)


def _stock_bit(value: int | None) -> str:
    if value is None:
        return ""
    return "in stock" if value else "out of stock"
