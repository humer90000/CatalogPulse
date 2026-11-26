import tempfile
import unittest
from pathlib import Path

from catalogpulse.changes import diff, snap_of
from catalogpulse.export import write_outputs
from catalogpulse.parse import Item
from catalogpulse.store import Store


def item(key, price, stock=1):
    return Item(
        key=key,
        url=f"https://shop.example/{key}",
        title=key,
        sku=None,
        category="Demo",
        image=None,
        currency="USD",
        price=price,
        price_text=f"${price:.2f}",
        in_stock=stock,
        stock_text="In stock" if stock else "Out of stock",
        extra={"rating": "4"},
    )


class StoreTests(unittest.TestCase):
    def test_xlsx_strips_control_characters(self):
        row = item("lamp", 10)
        row.title = "bad\x0btitle"
        with tempfile.TemporaryDirectory() as tmp:
            write_outputs(
                Path(tmp),
                {
                    "site": "shop",
                    "run_id": 1,
                    "started_at": "2026-11-26T10:00:00Z",
                    "pages": 1,
                    "failed": 0,
                    "complete": True,
                },
                [row],
                [],
                {"xlsx"},
            )
    def test_second_run_sees_the_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "catalog.db")
            try:
                store.commit_run(
                    "shop",
                    [item("lamp", 10), item("shade", 4)],
                    started_at="2026-11-26T10:00:00Z",
                    pages=1,
                    failed=0,
                    elapsed_ms=200,
                    threshold=0,
                    complete=True,
                )
                store.commit_run(
                    "shop",
                    [item("lamp", 12, stock=0)],
                    started_at="2026-11-26T11:00:00Z",
                    pages=1,
                    failed=0,
                    elapsed_ms=180,
                    threshold=0,
                    complete=True,
                )
                run, items, previous = store.history("shop")
                self.assertEqual(len(items), 1)
                self.assertEqual(items[0].price, 12)
                self.assertEqual(items[0].extra["rating"], "4")
                self.assertEqual(set(previous), {"lamp", "shade"})
                changes = diff(
                    previous,
                    {row.key: snap_of(row) for row in items},
                    float(run["threshold"]),
                    bool(run["complete"]),
                )
                kinds = sorted(change.kind for change in changes)
                self.assertEqual(kinds, ["missing", "stock", "up"])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
