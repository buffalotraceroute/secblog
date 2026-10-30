"""Resumable historical backfill: paginated feeds, sitemaps, Wayback CDX."""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlsplit

from . import store
from .feeds import parse_feed
from .http import Gate, get, make_client
from .sources import Source, load_sources
from .tagger import tag_post
from .util import (
    clean_url,
    force_https,
    iso,
    make_excerpt,
    now_utc,
    parse_date,
    title_from_url,
    wayback_url,
)

# Soft deadline so a single Action / local run does not run forever.
DEFAULT_BUDGET_S = 35 * 60


def _platform(src: Source) -> str:
    return src.detected_platform()


async def page_blogspot(client, gate, src, state, posts_map, budget) -> int:
    added = 0
    feed = next((f for f in src.feeds if "feeds/posts" in f), None) or (src.feeds[0] if src.feeds else None)
    if not feed:
        return 0
    start = int((state.get(src.id) or {}).get("blogspot_start", 1))
    page_size = 150
    while time.monotonic() < budget:
        url = feed
        sep = "&" if "?" in url else "?"
        if "start-index=" not in url:
            url = f"{url}{sep}start-index={start}&max-results={page_size}"
        else:
            url = re.sub(r"start-index=\d+", f"start-index={start}", url)
        async with gate:
            await gate.wait_host(urlsplit(url).hostname or "")
            r = await get(client, force_https(url), browser_ua=True)
        if r is None or r.status_code != 200:
            break
        posts = parse_feed(r.content, blog_id=src.id, via="feed-paged")
        if not posts:
            break
        for p in posts:
            tag_post(p, blog_default=src.category)
            if store.merge_post(posts_map, p):
                added += 1
        if len(posts) < page_size:
            state.setdefault(src.id, {})["blogspot_done"] = True
            break
        start += page_size
        state.setdefault(src.id, {})["blogspot_start"] = start
    return added


async def page_wordpress(client, gate, src, state, posts_map, budget) -> int:
    """WordPress: try REST API first, then ?paged=N on the feed."""
    added = 0
    host = urlsplit(src.homepage).scheme + "://" + (urlsplit(src.homepage).hostname or "")
    page = int((state.get(src.id) or {}).get("wp_page", 1))
    # REST API
    while time.monotonic() < budget and page <= 200:
        api = f"{host}/wp-json/wp/v2/posts?per_page=100&page={page}&_fields=link,title,date_gmt,excerpt,author_name,tags"
        async with gate:
            await gate.wait_host(urlsplit(host).hostname or "")
            r = await get(client, api, browser_ua=True)
        if r is None or r.status_code in (404, 401, 403) or (r.status_code == 400 and page > 1):
            break
        if r.status_code != 200:
            break
        try:
            data = r.json()
        except Exception:
            break
        if not isinstance(data, list) or not data:
            state.setdefault(src.id, {})["wp_done"] = True
            break
        for item in data:
            url = clean_url(item.get("link") or "")
            if not url:
                continue
            title = item.get("title")
            if isinstance(title, dict):
                title = title.get("rendered")
            excerpt = item.get("excerpt")
            if isinstance(excerpt, dict):
                excerpt = excerpt.get("rendered")
            published = parse_date(item.get("date_gmt") or item.get("date"))
            p = {
                "url": url,
                "title": (title or title_from_url(url))[:300],
                "blog": src.id,
                "author": item.get("author_name"),
                "published": iso(published) if published else None,
                "fetched": iso(now_utc()),
                "excerpt": make_excerpt(excerpt or ""),
                "tags": [],
                "archive_url": wayback_url(url),
                "via": "wp-api",
            }
            tag_post(p, blog_default=src.category)
            if store.merge_post(posts_map, p):
                added += 1
        if len(data) < 100:
            state.setdefault(src.id, {})["wp_done"] = True
            break
        page += 1
        state.setdefault(src.id, {})["wp_page"] = page

    # Feed paging fallback
    if not (state.get(src.id) or {}).get("wp_done") and src.feeds:
        feed = src.feeds[0]
        paged = int((state.get(src.id) or {}).get("feed_page", 1))
        while time.monotonic() < budget and paged <= 100:
            url = f"{feed}{'&' if '?' in feed else '?'}paged={paged}"
            async with gate:
                await gate.wait_host(urlsplit(url).hostname or "")
                r = await get(client, force_https(url))
            if r is None or r.status_code != 200:
                break
            posts = parse_feed(r.content, blog_id=src.id, via="feed-paged")
            if not posts:
                break
            new_here = 0
            for p in posts:
                tag_post(p, blog_default=src.category)
                if store.merge_post(posts_map, p):
                    added += 1
                    new_here += 1
            if new_here == 0 and paged > 2:
                break
            paged += 1
            state.setdefault(src.id, {})["feed_page"] = paged
    return added


async def from_sitemap(client, gate, src, state, posts_map, budget) -> int:
    added = 0
    candidates = []
    if src.sitemap:
        candidates.append(src.sitemap)
    host = urlsplit(src.homepage)
    root = f"{host.scheme}://{host.hostname}/"
    candidates += [urljoin(root, p) for p in ("sitemap.xml", "sitemap_index.xml", "wp-sitemap.xml",
                                              "post-sitemap.xml", "sitemap-posts.xml", "sitemap/sitemap.xml")]
    seen_maps: set[str] = set()
    urls: list[tuple[str, str | None]] = []

    async def fetch_map(url: str, depth: int = 0):
        if url in seen_maps or depth > 2 or time.monotonic() >= budget:
            return
        seen_maps.add(url)
        async with gate:
            await gate.wait_host(urlsplit(url).hostname or "")
            r = await get(client, force_https(url), browser_ua=True)
        if r is None or r.status_code != 200 or not r.content.startswith((b"<?", b"<")):
            return
        try:
            root_el = ET.fromstring(r.content)
        except ET.ParseError:
            return
        ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root_el.findall(".//s:sitemap/s:loc", ns) + root_el.findall(".//{*}sitemap/{*}loc"):
            await fetch_map(loc.text.strip(), depth + 1)
        for url_el in root_el.findall("s:url", ns) + root_el.findall("{*}url"):
            loc = url_el.find("s:loc", ns)
            if loc is None:
                loc = url_el.find("{*}loc")
            last = url_el.find("s:lastmod", ns)
            if last is None:
                last = url_el.find("{*}lastmod")
            if loc is not None and loc.text:
                urls.append((loc.text.strip(), last.text.strip() if last is not None and last.text else None))

    for c in candidates:
        await fetch_map(c)
        if urls:
            break

    # Keep only likely post URLs (heuristic).
    skip = re.compile(r"/(tag|category|categories|author|page|wp-content|attachment|feed)/", re.I)
    keep = re.compile(r"/(20\d{2}/|/posts?/|/blog/|/research/|/article|/advisory|/write.?up|/news/)", re.I)
    filtered = []
    for u, lm in urls:
        path = urlsplit(u).path
        if skip.search(path):
            continue
        if keep.search(path) or re.search(r"/20\d{2}/\d{2}/", path) or path.count("/") >= 2:
            filtered.append((u, lm))
    # Cap per source so a huge corporate sitemap does not blow the budget.
    filtered = filtered[:2500]
    state.setdefault(src.id, {})["sitemap_urls"] = len(filtered)

    for u, lm in filtered:
        if time.monotonic() >= budget:
            break
        url = clean_url(u)
        published = parse_date(lm)
        p = {
            "url": url,
            "title": title_from_url(url),
            "blog": src.id,
            "author": None,
            "published": iso(published) if published else None,
            "fetched": iso(now_utc()),
            "excerpt": "",
            "tags": [],
            "archive_url": wayback_url(url),
            "via": "sitemap",
        }
        tag_post(p, blog_default=src.category)
        if store.merge_post(posts_map, p):
            added += 1
    return added


async def from_wayback(client, gate, src, state, posts_map, budget) -> int:
    """For Dead blogs: discover URLs via the Wayback CDX API. Never hit the live domain."""
    added = 0
    domain = src.wayback_domain or (urlsplit(src.homepage).hostname or "").removeprefix("www.")
    if not domain:
        return 0
    resume = (state.get(src.id) or {}).get("cdx_resume", "")
    # Prefer HTML captures; collapse to unique originals.
    params = (
        f"url={quote(domain + '/*')}&output=json&fl=original,timestamp,statuscode,mimetype"
        f"&filter=statuscode:200&filter=mimetype:text/html&collapse=urlkey&limit=1500"
    )
    if resume:
        params += f"&resumeKey={quote(resume)}"
    if src.wayback_before:
        params += f"&to={src.wayback_before}"
    cdx = f"https://web.archive.org/cdx/search/cdx?{params}"
    async with gate:
        await gate.wait_host("web.archive.org")
        r = await get(client, cdx, browser_ua=True)
    if r is None or r.status_code != 200:
        # Also try archive.org (sometimes different edge).
        cdx2 = cdx.replace("https://web.archive.org/", "https://archive.org/wayback/")
        async with gate:
            r = await get(client, cdx2, browser_ua=True)
        if r is None or r.status_code != 200:
            state.setdefault(src.id, {})["cdx_error"] = f"HTTP {getattr(r,'status_code',None)}"
            return 0
    try:
        data = r.json()
    except Exception:
        state.setdefault(src.id, {})["cdx_error"] = "bad json"
        return 0
    if not data or len(data) < 2:
        state.setdefault(src.id, {})["cdx_done"] = True
        return 0
    header, rows = data[0], data[1:]
    # Resume key may be returned via header
    resume_key = r.headers.get("x-resume-key") or ""
    skip = re.compile(r"/(tag|category|author|page|wp-content|wp-includes|feed)/|\.(css|js|png|jpe?g|gif|svg|ico|xml|json)$", re.I)
    for row in rows:
        if time.monotonic() >= budget:
            break
        rec = dict(zip(header, row))
        original = rec.get("original") or ""
        ts = rec.get("timestamp") or ""
        if not original or skip.search(original):
            continue
        # Drop likely index / home pages with no slug
        path = urlsplit(original).path.rstrip("/")
        if not path or path in ("", "/blog", "/blogs", "/research", "/posts"):
            continue
        url = clean_url(original)
        # Snapshot-specific archive URL; never link the live (possibly hijacked) domain as primary.
        arch = wayback_url(url, ts)
        published = None
        if len(ts) >= 8:
            published = parse_date(f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}")
        p = {
            "url": arch,  # primary link is the snapshot for dead blogs
            "title": title_from_url(url),
            "blog": src.id,
            "author": None,
            "published": iso(published) if published else None,
            "fetched": iso(now_utc()),
            "excerpt": f"Archived snapshot of {url} (original site is offline or hijacked).",
            "tags": ["wayback"],
            "archive_url": arch,
            "via": "wayback-cdx",
        }
        # Also keep original URL in tags for searchability
        tag_post(p, blog_default=src.category or "News & Industry")
        if store.merge_post(posts_map, p):
            added += 1

    st = state.setdefault(src.id, {})
    if resume_key:
        st["cdx_resume"] = resume_key
    else:
        st["cdx_done"] = True
    st["cdx_last"] = iso(now_utc())
    return added


async def wayback_feed_snapshot(client, gate, src, state, posts_map, budget) -> int:
    """If a Dead blog had a known feed, try the latest Wayback snapshot of that feed."""
    added = 0
    for feed in src.feeds:
        if time.monotonic() >= budget:
            break
        avail = f"https://archive.org/wayback/available?url={quote(feed)}"
        async with gate:
            await gate.wait_host("archive.org")
            r = await get(client, avail)
        if r is None or r.status_code != 200:
            continue
        try:
            snap = r.json().get("archived_snapshots", {}).get("closest", {})
        except Exception:
            continue
        if not snap.get("available"):
            continue
        snap_url = snap["url"]
        async with gate:
            await gate.wait_host("web.archive.org")
            fr = await get(client, snap_url, browser_ua=True)
        if fr is None or fr.status_code != 200:
            continue
        posts = parse_feed(fr.content, blog_id=src.id, via="wayback-feed", archive=True)
        for p in posts:
            # Rewrite live URLs to Wayback for dead blogs
            live = p["url"]
            p["archive_url"] = wayback_url(live)
            p["url"] = p["archive_url"]
            tag_post(p, blog_default=src.category)
            if store.merge_post(posts_map, p):
                added += 1
    return added


async def backfill_source(client, gate, src: Source, state: dict, budget: float) -> int:
    if (state.get(src.id) or {}).get("done"):
        return 0
    posts_map = store.load_blog(src.id)
    added = 0
    try:
        if src.is_dead:
            added += await wayback_feed_snapshot(client, gate, src, state, posts_map, budget)
            added += await from_wayback(client, gate, src, state, posts_map, budget)
        else:
            plat = _platform(src)
            if plat == "blogspot" or any("feeds/posts" in f for f in src.feeds):
                added += await page_blogspot(client, gate, src, state, posts_map, budget)
            if plat in ("wordpress", "generic", "medium", "substack") or src.feeds:
                added += await page_wordpress(client, gate, src, state, posts_map, budget)
            if src.sitemap or plat in ("generic", "wordpress"):
                added += await from_sitemap(client, gate, src, state, posts_map, budget)
        # Mark done when platform-specific cursors say so, or after a successful dead CDX pass.
        st = state.setdefault(src.id, {})
        if src.is_dead and st.get("cdx_done"):
            st["done"] = True
        if not src.is_dead and (st.get("blogspot_done") or st.get("wp_done")):
            st["done"] = True
    finally:
        store.save_blog(src.id, posts_map)
    return added


async def run(
    budget_s: float = DEFAULT_BUDGET_S,
    concurrency: int = 10,
    only: list[str] | None = None,
    prefer_dead: bool = False,
) -> dict:
    sources = load_sources()
    if only:
        sources = [s for s in sources if s.id in only]
    # Process live blogs with feeds first (high yield), then dead (Wayback), unless prefer_dead.
    def key(s: Source):
        if prefer_dead:
            return (0 if s.is_dead else 1, 0 if s.feeds else 1, s.id)
        return (1 if s.is_dead else 0, 0 if s.feeds else 1, s.id)

    sources = sorted([s for s in sources if s.backfill], key=key)
    state = store.load_json("backfill.json")
    gate = Gate(concurrency=concurrency, per_host=0.35)
    budget = time.monotonic() + budget_s
    summary = {"sources_attempted": 0, "sources_with_new": 0, "posts_new": 0, "errors": {}}

    async with make_client(concurrency) as client:
        for src in sources:
            if time.monotonic() >= budget:
                break
            summary["sources_attempted"] += 1
            try:
                n = await backfill_source(client, gate, src, state, budget)
                summary["posts_new"] += n
                if n:
                    summary["sources_with_new"] += 1
            except Exception as e:
                summary["errors"][src.id] = f"{type(e).__name__}: {e}"
            # Checkpoint frequently so a killed run still keeps progress.
            if summary["sources_attempted"] % 5 == 0:
                store.save_json("backfill.json", state)

    store.save_json("backfill.json", state)
    store.save_json("last_backfill.json", {"finished": iso(now_utc()), "summary": summary})
    return summary
