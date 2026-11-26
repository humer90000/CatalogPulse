import unittest

from catalogpulse.save import Snap, diff, render


def snap(key, price, stock=1, title=None):
    return Snap(key=key, title=title or key, url=f"https://shop.example/{key}", price=price, in_stock=stock)


class DiffTests(unittest.TestCase):
    def test_baseline_is_all_new(self):
        current = {"a": snap("a", 10)}
        changes = diff({}, current, threshold=0, track_missing=True)
        self.assertEqual([c.kind for c in changes], ["new"])

    def test_threshold_and_stock(self):
        previous = {"a": snap("a", 100), "b": snap("b", 50, stock=1)}
        current = {"a": snap("a", 102), "b": snap("b", 50, stock=0)}
        quiet = diff(previous, current, threshold=3, track_missing=True)
        self.assertEqual([c.kind for c in quiet], ["stock"])
        loud = diff(previous, current, threshold=1, track_missing=True)
        self.assertEqual(sorted(c.kind for c in loud), ["stock", "up"])
        up = next(c for c in loud if c.kind == "up")
        self.assertAlmostEqual(up.pct, 2.0)

    def test_missing_only_on_a_finished_crawl(self):
        previous = {"a": snap("a", 10), "b": snap("b", 12)}
        current = {"a": snap("a", 10)}
        self.assertEqual(
            [c.kind for c in diff(previous, current, 0, track_missing=False)],
            [],
        )
        gone = diff(previous, current, 0, track_missing=True)
        self.assertEqual([c.kind for c in gone], ["missing"])
        self.assertEqual(gone[0].key, "b")

    def test_report_stays_short_on_a_baseline(self):
        text = render("books", 1, 50, 1000, 4.2, 0, True, [], True)
        self.assertIn("baseline", text)
        self.assertNotIn("A Light", text)
        quiet = render("books", 2, 50, 1000, 3.1, 0, True, [], False)
        self.assertIn("no movement", quiet)


if __name__ == "__main__":
    unittest.main()
