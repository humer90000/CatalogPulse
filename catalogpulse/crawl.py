from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from catalogpulse.config import ConfigError, Site
from catalogpulse.parse import (
    Item,
    canonical_listing,
    expand_template,
    linked_pages,
    page_cap,
    pager_selectors,
    parse_listing,
    read_page_total,
)
from catalogpulse.save import Store, diff, render, snap_of, utcnow, write_outputs

log = logging.getLogger("catalogpulse")

# Practice catalogues don't check the TLS fingerprint. Three real UAs are plenty.
_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
)

_RETRY = {429, 500, 502, 503, 504}


class FetchError(Exception):
    pass


class CrawlError(Exception):
    pass


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, site: Site):
        self.client = client
        self.site = site

    async def text(self, url: str) -> str:
        last = "no response"
        for attempt in range(self.site.retries):
            await self._pause()
            try:
                response = await self.client.get(url, headers=self._headers())
            except httpx.TransportError as exc:
                last = str(exc).splitlines()[0][:180]
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            if response.status_code in _RETRY:
                last = f"HTTP {response.status_code}"
                wait = _retry_wait(response) or 0.5 * (attempt + 1)
                await asyncio.sleep(min(wait, 15))
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} for {url}")
            return response.text
        raise FetchError(f"{url} failed after {self.site.retries} tries ({last})")

    async def many(self, urls: list[str]) -> list[tuple[str, str | None, str | None]]:
        total = len(urls)
        if not total:
            return []
        done = 0
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(self.site.concurrency)

        async def one(url: str):
            nonlocal done
            async with sem:
                try:
                    html = await self.text(url)
                    err = None
                except FetchError as exc:
                    html = None
                    err = str(exc)
            async with lock:
                done += 1
                if done == total or done % 10 == 0:
                    log.info("%s  fetched %s/%s", self.site.name, done, total)
            return url, html, err

        return list(await asyncio.gather(*(one(url) for url in urls)))

    async def robots(self):
        if not self.site.obey_robots:
            return None
        parts = urlsplit(self.site.start_url)
        url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        try:
            response = await self.client.get(url, headers={"User-Agent": _UAS[0]})
        except httpx.HTTPError as exc:
            log.warning("%s  robots.txt skipped (%s)", self.site.name, exc.__class__.__name__)
            return None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            log.warning("%s  robots.txt HTTP %s", self.site.name, response.status_code)
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    async def _pause(self) -> None:
        lo, hi = self.site.delay
        if hi <= 0:
            return
        await asyncio.sleep(random.uniform(lo, hi))

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(_UAS),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        }
        headers.update(self.site.headers)
        return headers


def allowed(parser: RobotFileParser | None, url: str) -> bool:
    if parser is None:
        return True
    return parser.can_fetch(_UAS[0], url)


def _retry_wait(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


@dataclass(slots=True)
class Crawl:
    items: dict[str, Item]
    pages: int
    failed: list[str] = field(default_factory=list)
    complete: bool = True
    elapsed: float = 0.0


async def crawl(site: Site) -> Crawl:
    started = time.perf_counter()
    timeout = httpx.Timeout(site.timeout, connect=min(10.0, site.timeout))
    limits = httpx.Limits(
        max_connections=site.concurrency,
        max_keepalive_connections=site.concurrency,
    )
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout, limits=limits) as client:
        fetcher = Fetcher(client, site)
        robots = await fetcher.robots()
        if not allowed(robots, site.start_url):
            raise CrawlError(f"robots.txt disallows {site.start_url}")
        try:
            first_html = await fetcher.text(site.start_url)
        except FetchError as exc:
            raise CrawlError(str(exc)) from exc

        preview = parse_listing(first_html, site.start_url, site)
        log.info("%s  %s  %s cards", site.name, site.start_url, len(preview))
        if not preview:
            return Crawl(items={}, pages=1, complete=False, elapsed=time.perf_counter() - started)

        pages: list[tuple[str, str]] = [(site.start_url, first_html)]
        seen = {canonical_listing(site.start_url)}
        failed: list[str] = []
        complete = True

        total = None
        if site.pager.url_template and site.pager.page_count:
            total = read_page_total(first_html, site.pager.page_count)

        if total:
            cap = page_cap(site)
            queued, clipped = expand_template(site.pager.url_template, site.start_url, total, cap)
            if clipped:
                complete = False
                log.info("%s  stopped at %s pages", site.name, cap)
            else:
                log.info("%s  pager says %s pages", site.name, total)
            queued, blocked = _keep(queued, seen, robots, site)
            if blocked:
                complete = False
            if queued:
                got = await _take(fetcher, queued, pages, seen, failed)
                if len(got) != len(queued):
                    complete = False
        elif pager_selectors(site):
            complete = await _follow(fetcher, site, robots, pages, seen, failed)
        else:
            log.info("%s  single page", site.name)

        items: dict[str, Item] = {}
        dupes = 0
        for url, html in pages:
            for item in parse_listing(html, url, site):
                if item.key in items:
                    dupes += 1
                items[item.key] = item
        if dupes:
            log.debug("%s  %s duplicate cards", site.name, dupes)

    return Crawl(
        items=items,
        pages=len(pages),
        failed=failed,
        complete=complete and not failed,
        elapsed=time.perf_counter() - started,
    )


async def execute(site: Site, db_path: Path, export_dir: Path, formats: set[str]) -> tuple[int, str]:
    started_at = utcnow()
    result = await crawl(site)
    if not result.items:
        log.error("%s  0 cards. the item selector probably does not match", site.name)
        return 1, ""
    store = Store(db_path)
    try:
        store.commit_run(
            site.name,
            list(result.items.values()),
            started_at=started_at,
            pages=result.pages,
            failed=len(result.failed),
            elapsed_ms=int(result.elapsed * 1000),
            threshold=site.price_change_pct,
            complete=result.complete,
        )
        text = report_run(store, site.name, export_dir, formats)
    finally:
        store.close()
    return 0, text


def report_run(store: Store, site: str, export_dir: Path | None, formats: set[str]) -> str:
    run, items, previous = store.history(site)
    if run is None:
        raise ConfigError(f"no runs stored for {site}")
    baseline = not previous
    index = store.run_index(site, int(run["id"]))
    changes = diff(
        previous,
        {item.key: snap_of(item) for item in items},
        float(run["threshold"]),
        bool(run["complete"]) and not baseline,
    )
    text = render(
        site,
        index,
        int(run["pages"]),
        len(items),
        (run["elapsed_ms"] or 0) / 1000,
        int(run["failed"]),
        bool(run["complete"]),
        changes,
        baseline,
    )
    if export_dir is not None and formats:
        meta = {
            "site": site,
            "run_id": index,
            "started_at": run["started_at"],
            "pages": int(run["pages"]),
            "failed": int(run["failed"]),
            "complete": bool(run["complete"]),
        }
        paths = write_outputs(Path(export_dir) / site, meta, items, changes, formats)
        if paths:
            text += "\n" + "\n".join(f"  {_rel(path)}" for path in paths)
    return text


async def _follow(fetcher, site: Site, robots, pages, seen: set[str], failed: list[str]) -> bool:
    selectors = pager_selectors(site)
    pending = _fresh(linked_pages(pages[0][1], pages[0][0], selectors), seen)
    if not pending:
        log.info("%s  no more pages from the pager", site.name)
        return True
    complete = True
    while pending:
        room = page_cap(site) - len(seen)
        if room <= 0:
            return False
        if len(pending) > room:
            pending = pending[:room]
            complete = False
        queued, blocked = _keep(pending, seen, robots, site)
        if blocked:
            complete = False
        if not queued:
            return complete
        got = await _take(fetcher, queued, pages, seen, failed)
        if len(got) != len(queued):
            complete = False
        found: list[str] = []
        known = set(seen)
        for url, html in got:
            for link in linked_pages(html, url, selectors):
                key = canonical_listing(link)
                if key in known:
                    continue
                known.add(key)
                found.append(link)
        pending = found
    return complete


async def _take(fetcher: Fetcher, urls, pages, seen: set[str], failed: list[str]):
    got = []
    batch = await fetcher.many(urls)
    for url, html, err in batch:
        key = canonical_listing(url)
        if err or html is None:
            seen.add(key)
            failed.append(url)
            log.warning("%s  %s", fetcher.site.name, err or url)
            continue
        if key in seen:
            continue
        seen.add(key)
        pages.append((url, html))
        got.append((url, html))
    return got


def _fresh(urls: list[str], seen: set[str]) -> list[str]:
    fresh = []
    for url in urls:
        if canonical_listing(url) in seen:
            continue
        fresh.append(url)
    return fresh


def _keep(urls, seen, robots, site: Site):
    kept = []
    blocked = 0
    local = set(seen)
    for url in urls:
        key = canonical_listing(url)
        if key in local:
            continue
        local.add(key)
        if not allowed(robots, url):
            blocked += 1
            continue
        kept.append(url)
    if blocked:
        log.warning("%s  robots.txt skipped %s url(s)", site.name, blocked)
    return kept, blocked


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)
