from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from catalogpulse.changes import Snap, snap_of
from catalogpulse.parse import Item

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
                [_row(run_id, site, item) for item in items],
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

    def history(self, site: str) -> tuple[sqlite3.Row | None, list[Item], dict[str, Snap]]:
        run = self._run(site, before=None)
        if run is None:
            return None, [], {}
        items = self.items_for(int(run["id"]))
        prior = self._run(site, before=int(run["id"]))
        previous: dict[str, Snap] = {}
        if prior is not None:
            for item in self.items_for(int(prior["id"])):
                previous[item.key] = snap_of(item)
        return run, items, previous

    def items_for(self, run_id: int) -> list[Item]:
        rows = self.conn.execute(
            "SELECT * FROM observations WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
        return [_item(row) for row in rows]

    def _run(self, site: str, before: int | None) -> sqlite3.Row | None:
        if before is None:
            row = self.conn.execute(
                """
                SELECT * FROM runs
                WHERE site = ? AND finished_at IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (site,),
            ).fetchone()
            return row
        return self.conn.execute(
            """
            SELECT * FROM runs
            WHERE site = ? AND finished_at IS NOT NULL AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (site, before),
        ).fetchone()


def _row(run_id: int, site: str, item: Item) -> tuple:
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


def _item(row: sqlite3.Row) -> Item:
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
