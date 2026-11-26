from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from catalogpulse.changes import diff, render, snap_of
from catalogpulse.config import ConfigError, Site
from catalogpulse.export import write_outputs
from catalogpulse.fetch import FetchError, Fetcher, allowed
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
from catalogpulse.store import Store, utcnow

log = logging.getLogger("catalogpulse")


class CrawlError(Exception):
    pass


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
            return Crawl(items={}, pages=1, complete=False, elapsed=_elapsed(started))

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
            queued, blocked = _allowed(queued, seen, robots, site)
            if blocked:
                complete = False
            if queued:
                got = await _take(fetcher, queued, pages, seen, failed)
                if len(got) != len(queued):
                    complete = False
        elif pager_selectors(site):
            complete = await _waves(fetcher, site, robots, pages, seen, failed)
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
        elapsed=_elapsed(started),
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
        text = materialize(store, site.name, export_dir, formats)
    finally:
        store.close()
    return 0, text


def materialize(store: Store, site: str, export_dir: Path | None, formats: set[str]) -> str:
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


async def _waves(fetcher, site: Site, robots, pages, seen: set[str], failed: list[str]) -> bool:
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
        queued, blocked = _allowed(pending, seen, robots, site)
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


async def _take(
    fetcher: Fetcher,
    urls: list[str],
    pages: list[tuple[str, str]],
    seen: set[str],
    failed: list[str],
) -> list[tuple[str, str]]:
    got: list[tuple[str, str]] = []
    batch = await fetcher.many(urls)
    for url, html, err in batch:
        key = canonical_listing(url)
        if err or html is None:
            # Remember the miss so the next pager wave doesn't queue it again.
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
        key = canonical_listing(url)
        if key in seen:
            continue
        fresh.append(url)
    return fresh


def _allowed(urls: list[str], seen: set[str], robots, site: Site) -> tuple[list[str], int]:
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


def _elapsed(started: float) -> float:
    return time.perf_counter() - started
