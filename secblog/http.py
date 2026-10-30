"""Shared HTTP client: timeouts, UA rotation, polite concurrency helpers."""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .util import BROWSER_USER_AGENT, USER_AGENT

TIMEOUT = httpx.Timeout(connect=10.0, read=25.0, write=10.0, pool=30.0)


def make_client(concurrency: int = 16) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=True,
        verify=False,  # many research blogs have broken/expired certs
        limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/atom+xml,application/rss+xml,application/xml,text/xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )


async def get(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    browser_ua: bool = False,
) -> httpx.Response | None:
    hdrs = dict(headers or {})
    if browser_ua:
        hdrs["User-Agent"] = BROWSER_USER_AGENT
    try:
        r = await client.get(url, headers=hdrs)
        if r.status_code in (403, 429) and not browser_ua:
            return await get(client, url, headers=headers, browser_ua=True)
        return r
    except Exception:
        return None


class Gate:
    """Simple concurrency + per-host politeness limiter."""

    def __init__(self, concurrency: int = 12, per_host: float = 0.15):
        self.sem = asyncio.Semaphore(concurrency)
        self.per_host = per_host
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self.sem.acquire()
        return self

    async def __aexit__(self, *exc: Any):
        self.sem.release()

    async def wait_host(self, host: str) -> None:
        async with self._lock:
            import time
            now = time.monotonic()
            delay = self._last.get(host, 0) + self.per_host - now
            self._last[host] = max(now, self._last.get(host, 0) + self.per_host)
        if delay > 0:
            await asyncio.sleep(delay)
