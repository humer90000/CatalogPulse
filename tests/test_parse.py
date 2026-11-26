import unittest
from pathlib import Path

from catalogpulse.config import FieldSpec, Site, load_site
from catalogpulse.parse import (
    canonical_listing,
    expand_template,
    linked_pages,
    parse_listing,
    read_page_total,
    split_price,
    stock_flag,
)

ROOT = Path(__file__).resolve().parents[1]

BOOK = """
<article class="product_pod">
  <a href="a-light-in-the-attic_1000/index.html">
    <img src="../media/cache/x.jpg" class="thumbnail">
  </a>
  <p class="star-rating Three"></p>
  <h3><a href="a-light-in-the-attic_1000/index.html" title="A Light in the Attic">A Light in the ...</a></h3>
  <p class="price_color">£51.77</p>
  <p class="instock availability">In stock</p>
</article>
<ul class="pager"><li class="current">Page 1 of 3</li></ul>
"""


def site(**kwargs) -> Site:
    fields = kwargs.pop(
        "fields",
        {
            "title": FieldSpec("h3 a", attr="title"),
            "url": FieldSpec("h3 a", attr="href"),
            "price": FieldSpec("p.price_color"),
            "stock": FieldSpec("p.availability"),
            "image": FieldSpec("img.thumbnail", attr="src"),
            "rating": FieldSpec(
                "p.star-rating",
                attr="class",
                map={"One": "1", "Two": "2", "Three": "3", "Four": "4", "Five": "5"},
            ),
        },
    )
    base = dict(
        name="books",
        start_url="https://books.toscrape.com/catalogue/page-1.html",
        item="article.product_pod",
        fields=fields,
        category="Books",
        jsonld=False,
    )
    base.update(kwargs)
    return Site(**base)


class MoneyTests(unittest.TestCase):
    def test_common_shapes(self):
        self.assertEqual(split_price("£51.77"), (51.77, "GBP"))
        self.assertEqual(split_price("$1,299.00"), (1299.0, "USD"))
        self.assertEqual(split_price("1.299,50 €"), (1299.5, "EUR"))
        self.assertEqual(split_price("1,299"), (1299.0, None))
        self.assertEqual(split_price("51,77"), (51.77, None))
        self.assertEqual(split_price("  "), (None, None))
        self.assertEqual(split_price("Was £51.77 Now £45.00"), (45.0, "GBP"))
        self.assertEqual(split_price("£12.99 (4.5)")[0], 12.99)

    def test_stock_phrases(self):
        self.assertEqual(stock_flag("In stock (22 available)")[:1], (1,))
        self.assertEqual(stock_flag("Out of stock")[0], 0)
        self.assertEqual(stock_flag("Unavailable")[0], 0)
        self.assertEqual(stock_flag("нет в наличии")[0], 0)
        self.assertEqual(stock_flag("в наличии")[0], 1)
        self.assertIsNone(stock_flag("ask the shop")[0])


class ListingTests(unittest.TestCase):
    def test_book_card(self):
        page = "https://books.toscrape.com/catalogue/page-1.html"
        items = parse_listing(BOOK, page, site())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.title, "A Light in the Attic")
        self.assertEqual(item.price, 51.77)
        self.assertEqual(item.currency, "GBP")
        self.assertEqual(item.in_stock, 1)
        self.assertEqual(item.extra["rating"], "3")
        self.assertEqual(
            item.url,
            "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html",
        )
        self.assertEqual(item.image, "https://books.toscrape.com/media/cache/x.jpg")
        self.assertEqual(item.key, item.url)

    def test_jsonld_fills_a_missing_price_and_does_not_override(self):
        html = """
        <script type="application/ld+json">
        {"@type":"Product","name":"Lamp","sku":"L-1",
         "url":"https://shop.example/lamp",
         "image":"/img/lamp.jpg",
         "offers":{"@type":"Offer","price":"12.50","priceCurrency":"USD",
                   "availability":"https://schema.org/InStock"}}
        </script>
        <article class="card">
          <a class="name" href="/lamp">Lamp</a>
          <span class="price">$9.00</span>
        </article>
        <article class="card">
          <a class="name" href="/shade">Shade</a>
          <span class="price"></span>
        </article>
        """
        shade = """
        <script type="application/ld+json">
        {"@type":"Product","name":"Shade","url":"https://shop.example/shade",
         "offers":{"price":"4.00","priceCurrency":"USD",
                   "availability":"https://schema.org/OutOfStock"}}
        </script>
        """
        page = "https://shop.example/list"
        parsed = parse_listing(html + shade, page, _shop())
        by_title = {item.title: item for item in parsed}
        self.assertEqual(by_title["Lamp"].price, 9.0)
        self.assertEqual(by_title["Lamp"].sku, "L-1")
        self.assertEqual(by_title["Shade"].price, 4.0)
        self.assertEqual(by_title["Shade"].in_stock, 0)
        self.assertEqual(by_title["Shade"].currency, "USD")

    def test_pager_template_and_links(self):
        total = read_page_total(BOOK, "ul.pager li.current")
        self.assertEqual(total, 3)
        start = "https://books.example/catalogue/page-1.html"
        urls, clipped = expand_template(
            "https://books.example/catalogue/page-{page}.html",
            start,
            total,
            cap=10,
        )
        self.assertFalse(clipped)
        self.assertEqual(
            urls,
            [
                "https://books.example/catalogue/page-2.html",
                "https://books.example/catalogue/page-3.html",
            ],
        )
        relative, clipped = expand_template(
            "page-{page}.html",
            "https://books.toscrape.com/catalogue/category/books/mystery_3/index.html",
            2,
            cap=10,
        )
        self.assertFalse(clipped)
        self.assertEqual(
            relative,
            ["https://books.toscrape.com/catalogue/category/books/mystery_3/page-2.html"],
        )
        count_html = "<p class='count'>Showing 1 to 20 of 1000 results</p>"
        self.assertIsNone(read_page_total(count_html, "p.count"))
        html = """
        <ul class="pagination">
          <li class="active"><span>1</span></li>
          <a class="page-link" href="?page=2">2</a>
          <a class="page-link" href="?page=1">1</a>
          <a class="page-link" href="?page=2">2 again</a>
        </ul>
        """
        links = linked_pages(html, "https://shop.example/laptops", ["ul.pagination a.page-link"])
        self.assertEqual(
            links,
            ["https://shop.example/laptops?page=2", "https://shop.example/laptops?page=1"],
        )

    def test_page_one_collapses(self):
        bare = "https://shop.example/laptops"
        paged = "https://shop.example/laptops?page=1"
        other = "https://shop.example/laptops?page=2"
        self.assertEqual(canonical_listing(bare), canonical_listing(paged))
        self.assertNotEqual(canonical_listing(bare), canonical_listing(other))

    def test_composite_key_keeps_seasons(self):
        html = """
        <table>
        <tr class="team"><td class="name">Boston Bruins</td><td class="year">1990</td></tr>
        <tr class="team"><td class="name">Boston Bruins</td><td class="year">1991</td></tr>
        </table>
        """
        parsed = parse_listing(
            html,
            "https://www.scrapethissite.com/pages/forms/",
            Site(
                name="hockey",
                start_url="https://www.scrapethissite.com/pages/forms/",
                item="tr.team",
                fields={"title": FieldSpec("td.name"), "year": FieldSpec("td.year")},
                key=("title", "year"),
                jsonld=False,
            ),
        )
        self.assertEqual(
            {item.key for item in parsed},
            {"Boston Bruins | 1990", "Boston Bruins | 1991"},
        )
        self.assertEqual(parsed[0].extra["year"], "1990")

    def test_demo_configs(self):
        books = load_site(ROOT / "sites" / "books.yaml")
        laptops = load_site(ROOT / "sites" / "laptops.yaml")
        self.assertEqual(books.name, "books")
        self.assertEqual(books.pager.url_template.count("{page}"), 1)
        self.assertFalse(books.jsonld)
        self.assertEqual(laptops.price_change_pct, 1)
        self.assertTrue(laptops.obey_robots)
        self.assertIn("description", laptops.fields)


def _shop() -> Site:
    return Site(
        name="shop",
        start_url="https://shop.example/list",
        item="article.card",
        fields={
            "title": FieldSpec("a.name"),
            "url": FieldSpec("a.name", attr="href"),
            "price": FieldSpec("span.price"),
        },
        jsonld=True,
    )


if __name__ == "__main__":
    unittest.main()
