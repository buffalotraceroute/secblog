"""Incremental feed collector with conditional requests."""
from __future__ import annotations

import asyncio
import traceback
from urllib.parse import urlsplit

from . import store
from .feeds import parse_feed
from .http import Gate, get, make_client
from .sources import Source, load_sources
from .tagger import tag_post
from .util import force_https, iso, now_utc


async def fetch_feed(client, gate: Gate, src: Source, feed_url: str, state: dict) -> tuple[list[dict], dict, str | None]:
    """Fetch one feed. Returns (posts, new_state_entry, error)."""
    host = urlsplit(feed_url).hostname or ""
    async with gate:
        await gate.wait_host(host)
        headers = {}
        prev = state.get(feed_url) or {}
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]
        r = await get(client, force_https(feed_url), headers=headers)
        if r is None:
            return [], prev, "network error"
        if r.status_code == 304:
            prev = {**prev, "last_checked": iso(now_utc()), "status": 304}
            return [], prev, None
        if r.status_code != 200:
            return [], {**prev, "last_checked": iso(now_utc()), "status": r.status_code}, f"HTTP {r.status_code}"
        posts = parse_feed(r.content, blog_id=src.id, via="feed")
        new = {
            "etag": r.headers.get("etag"),
            "last_modified": r.headers.get("last-modified"),
            "last_checked": iso(now_utc()),
            "status": 200,
            "entries": len(posts),
        }
        return posts, new, None


async def collect_source(client, gate, src: Source, state: dict, errors: dict) -> tuple[int, int]:
    """Poll all feeds for one source. Returns (new_or_updated, total_seen)."""
    if src.is_dead:
        return 0, 0
    if not src.feeds:
        errors[src.id] = "no feeds configured"
        return 0, 0
    posts_map = store.load_blog(src.id)
    added = 0
    seen = 0
    feed_errors = []
    for feed in src.feeds:
        try:
            posts, new_state, err = await fetch_feed(client, gate, src, feed, state)
            state[feed] = new_state
            if err:
                feed_errors.append(f"{feed}: {err}")
                continue
            for p in posts:
                seen += 1
                tag_post(p, blog_default=src.category)
                if store.merge_post(posts_map, p):
                    added += 1
        except Exception as e:
            feed_errors.append(f"{feed}: {type(e).__name__}: {e}")
    store.save_blog(src.id, posts_map)
    if feed_errors and added == 0 and seen == 0:
        errors[src.id] = "; ".join(feed_errors)[:400]
    elif feed_errors:
        errors[src.id] = "partial: " + "; ".join(feed_errors)[:400]
    return added, seen


async def run(concurrency: int = 16, only: list[str] | None = None) -> dict:
    sources = load_sources()
    if only:
        sources = [s for s in sources if s.id in only]
    state = store.load_json("etag.json")
    errors: dict[str, str] = {}
    gate = Gate(concurrency=concurrency, per_host=0.2)
    summary = {"sources": 0, "succeeded": 0, "failed": 0, "skipped_dead": 0, "posts_new": 0, "posts_seen": 0}

    async with make_client(concurrency) as client:
        async def one(src: Source):
            summary["sources"] += 1
            if src.is_dead:
                summary["skipped_dead"] += 1
                return
            try:
                added, seen = await collect_source(client, gate, src, state, errors)
                summary["posts_new"] += added
                summary["posts_seen"] += seen
                if src.id in errors and errors[src.id].startswith("no feeds"):
                    summary["failed"] += 1
                elif src.id in errors and not errors[src.id].startswith("partial"):
                    summary["failed"] += 1
                else:
                    summary["succeeded"] += 1
            except Exception as e:
                errors[src.id] = f"{type(e).__name__}: {e}"
                summary["failed"] += 1
                traceback.print_exc()

        await asyncio.gather(*(one(s) for s in sources))

    store.save_json("etag.json", state)
    store.save_json("last_run.json", {
        "finished": iso(now_utc()),
        "summary": summary,
        "errors": errors,
    })
    return {"summary": summary, "errors": errors}
