# CatalogPulse

Pulls a catalogue over HTTP and keeps every snapshot in SQLite. The next run prints what the price and the stock did. JSON, CSV and xlsx come out of the same database.

It fits a shop where the product cards are already in the HTML: a repeating block, a price, a link, and a way onto the next page. httpx fetches the pages, selectolax reads the cards. No browser. A new shop is a yaml file, not a new script. You set the card selector, the fields you care about, how a card is recognised next time, and which pager the site actually uses. Extra fields you add are stored and exported as their own columns.

A shop that draws the grid with JavaScript, or hides it behind a login, comes back empty. robots.txt is followed unless the yaml says `obey_robots: false`.

## Setup

Windows, from the repo root. `python` on this machine is sometimes the Store stub, so make the venv with `py -3`. After you activate it, `python` is the venv.

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

## Run

```
python -m catalogpulse run sites/books.yaml
```

The first run is just a baseline. Run it again and you should get `no movement` until a price or a stock flag actually changes.

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

`price_change_pct: 3` ignores a move under 3%. Stock flips are always listed. If the crawl stops early (`max_pages`, or a page failed) it will not call the unseen cards missing. That mistake is worse than a quiet report.

```
python -m catalogpulse show books
python -m catalogpulse export books --format xlsx
python -m catalogpulse run sites
```

`show` reprints the last comparison. `export` writes the files again from the database. A directory runs every yaml in it.

The Catalog sheet is the latest snapshot. Changes is only the diff, and a quiet second run leaves that sheet as a header row. CSV is UTF-8 with a BOM, because Excel on Windows still guesses the code page without it.

## Config

Copy `sites/books.yaml`. Selectors are CSS. A field is a selector string, or a map with `selector`, `attr` and `map`. The books rating lives in a class (`star-rating Three`), which is why that one has a map.

`key` is how a card is recognised next time. `url` is the usual choice. If one field is shared by a lot of rows (a team across seasons) pass a list and the parts are joined.

Whichever pagination block is set:

- `url_template` plus `page_count` reads "Page 1 of 50" and fetches `page-{page}.html` in parallel. A relative template is resolved against the start url. Page 1 is the start url itself.
- `page_links` takes every pager href, then any new ones those pages add.
- `next` follows one link, when the other two are absent.

It stops at 500 pages if a next link never dies. `max_pages` is the short cut when you want one on purpose.

Anything that is not title, url, price, stock, sku or image is stored and shows up as its own column. `jsonld: true` fills an empty price, sku, image or stock from Product JSON-LD when the CSS missed it. If both exist, the CSS value stays.

`£51.77`, `$1,299.00` and `1.299,50 €` become numbers. The original string stays in `price_raw`.

books.toscrape.com publishes no robots.txt. The laptop host disallows `/test-sites/product/`, so that config stays on the listing pages.

## Files

```
data/catalog.db
exports/<site>/latest.json
exports/<site>/latest.csv
exports/<site>/latest.xlsx
```

`data/` and `exports/` are gitignored. They show up in the directory you ran the command from.
