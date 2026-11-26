# CatalogPulse

HTTP catalogue crawler. One YAML file per shop. Every run is stored in SQLite, and the next run prints what happened to price and stock. The same rows are written to JSON, CSV, and an xlsx with two sheets, Catalog and Changes.

Pages are fetched with httpx and parsed with selectolax. Nothing launches a browser.

Measured on books.toscrape.com from this machine: 50 pages, 1,000 books, 3.1 seconds. `sites/laptops.yaml` is the same runner on a shop whose pager only shows a window of page links and reveals the rest after those pages are fetched (20 pages, 117 laptops).

A new shop is a new YAML file. The listing has to be in the HTML response. Pages that only fill in after JavaScript, pages behind a login, and URLs disallowed by robots.txt are out of scope. `obey_robots` defaults to true.

## Setup

Windows, from the repo root:

```
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS / Linux:

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows the `python` command is sometimes the Store stub. `py -3` is the interpreter you want for the venv. After activation, `python` is the venv.

## Crawl

```
python -m catalogpulse run sites/books.yaml
```

The first run is a baseline. Run it again and it should say `no movement` until a price or a stock flag actually changes.

```
13:22:40 books  https://books.toscrape.com/catalogue/page-1.html  20 cards
13:22:40 books  pager says 50 pages
13:22:41 books  fetched 10/49
13:22:42 books  fetched 49/49
books  run 1  50 pages  1000 items  3.1s
  baseline, 1000 new
  exports\books\latest.json
  exports\books\latest.csv
  exports\books\latest.xlsx

books  run 2  50 pages  1000 items  3.1s
  no movement
```

`price_change_pct` in the YAML is the noise filter. `3` ignores a move under 3%. Stock flips are always reported. A missing card is reported after a crawl that reached the last page. `max_pages` and a failed page both leave unseen cards out of that list.

```
python -m catalogpulse show books
python -m catalogpulse export books --format xlsx
python -m catalogpulse run sites
```

`show` reprints the last comparison. `export` rebuilds the files from the database. Passing a directory crawls every yaml in it.

The xlsx Catalog sheet is the latest snapshot (title, price, currency, stock, raw price text, url, and any extra field you listed). The Changes sheet is only the diff for that run. After a quiet second run the Changes sheet is just the header, which is what you want.

CSV is UTF-8 with a BOM, because Excel on Windows otherwise guesses the code page.

## Config

Copy `sites/books.yaml` or `sites/laptops.yaml`.

Selectors are CSS. A field is either a selector string or a map with `selector`, optional `attr`, and optional `map`. The books rating is a class (`star-rating Three`), so that field maps the word to a number.

`key` is how a card is recognized on the next run. Leave it on `url`. A list of fields is joined when one field is shared by many rows, such as a team and a season.

Pagination uses the first rule that matches:

- `url_template` and `page_count` read "Page 1 of 50", then fetch `page-{page}.html` together
- `page_links` collects pager hrefs, then any new hrefs those pages reveal
- `next` follows one link at a time when the other two are not set

There is a hard stop at 500 pages if a next link never ends. Set `max_pages` when you want a shorter run.

Fields besides title, url, price, stock, sku, and image are stored and exported as their own columns. `jsonld: true` fills an empty price, sku, image, or stock from Product JSON-LD. A value already read from CSS stays as it was.

`obey_robots` defaults to true. books.toscrape.com publishes no robots.txt. The laptop config requests listing pages only; that host disallows `/test-sites/product/`.

Prices such as `£51.77`, `$1,299.00`, and `1.299,50 €` are parsed into a number. The original string is kept in `price_raw`.

## Where the data lands

```
data/catalog.db                  every snapshot
exports/<site>/latest.json       items and changes
exports/<site>/latest.csv        catalogue grid
exports/<site>/latest.xlsx       Catalog and Changes
```

`data/` and `exports/` stay on the machine that ran the crawl. They are listed in `.gitignore`.
