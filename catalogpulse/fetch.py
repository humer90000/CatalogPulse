from __future__ import annotations

import asyncio
import logging
import random
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from catalogpulse.config import Site

log = logging.getLogger("catalogpulse")

# A short, current-looking set. Rotating fifty fake strings does not help.
_UAS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) Gecko/20100101 Firefox/146.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
)

_RETRY = {429, 500, 502, 503, 504}


class FetchError(Exception):
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
                wait = _retry_after(response) or 0.5 * (attempt + 1)
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


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None
