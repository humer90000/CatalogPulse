from __future__ import annotations

from dataclasses import dataclass

from catalogpulse.parse import Item

_ORDER = {"up": 0, "down": 1, "stock": 2, "missing": 3, "new": 4}


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
            "old_stock": _boolify(self.old_stock),
            "new_stock": _boolify(self.new_stock),
        }


def snap_of(item: Item) -> Snap:
    return Snap(item.key, item.title, item.url, item.price, item.in_stock)


def diff(previous: dict[str, Snap], current: dict[str, Snap], threshold: float, track_missing: bool) -> list[Change]:
    changes: list[Change] = []
    for key, row in current.items():
        old = previous.get(key)
        if old is None:
            changes.append(
                Change(
                    "new",
                    key,
                    row.title,
                    row.url,
                    new_price=row.price,
                    new_stock=row.in_stock,
                )
            )
            continue
        old_cents, new_cents = _cents(old.price), _cents(row.price)
        if old_cents != new_cents and _worth(old.price, row.price, threshold):
            kind = _direction(old_cents, new_cents)
            changes.append(
                Change(
                    kind,
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
            changes.append(
                Change(
                    "missing",
                    key,
                    old.title,
                    old.url,
                    old_price=old.price,
                    old_stock=old.in_stock,
                )
            )
    changes.sort(key=lambda change: (_ORDER.get(change.kind, 9), (change.title or change.key).casefold()))
    return changes


def _worth(old: float | None, new: float | None, threshold: float) -> bool:
    if threshold <= 0:
        return True
    pct = _pct(old, new)
    if pct is None:
        return True
    return abs(pct) + 1e-9 >= threshold


def _direction(old_cents: int | None, new_cents: int | None) -> str:
    if old_cents is None:
        return "up"
    if new_cents is None or new_cents < old_cents:
        return "down"
    return "up"


def _pct(old: float | None, new: float | None) -> float | None:
    if old is None or new is None or abs(old) < 1e-9:
        return None
    return (new - old) / abs(old) * 100


def _cents(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(value * 100))


def _boolify(value: int | None):
    if value is None:
        return None
    return bool(value)


def render(name: str, run_id: int, pages: int, items: int, elapsed_s: float, failed: int, complete: bool, changes: list[Change], baseline: bool) -> str:
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
    lines.append(
        "  new {new}  up {up}  down {down}  stock {stock}  missing {missing}".format(**counts)
    )
    details = list(changes)
    if counts.get("new", 0) > 8:
        details = [change for change in changes if change.kind != "new"]
    cap = 20
    for change in details[:cap]:
        lines.append("  " + format_change(change))
    hidden = len(details) - cap
    if hidden > 0:
        lines.append(f"  ... {hidden} more in the xlsx")
    elif counts.get("new", 0) > 8:
        lines.append(f"  {counts['new']} new rows are in the export")
    return "\n".join(lines)


def format_change(change: Change) -> str:
    title = (change.title or change.key).replace("\n", " ")
    if len(title) > 64:
        title = title[:61] + "..."
    if change.kind in {"up", "down"}:
        pct = "" if change.pct is None else f" ({change.pct:+.1f}%)"
        return f"{change.kind:<7} {title}  {_money(change.old_price)} -> {_money(change.new_price)}{pct}"
    if change.kind == "stock":
        return f"stock   {title}  {_stock(change.old_stock)} -> {_stock(change.new_stock)}"
    if change.kind == "missing":
        return f"missing {title}  was {_money(change.old_price)}"
    return f"new     {title}  {_money(change.new_price)}"


def _money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _stock(value: int | None) -> str:
    if value is None:
        return "-"
    return "in" if value else "out"
