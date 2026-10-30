"""Feedly public stream cache: fallback for feeds behind Cloudflare/bot walls.

Feedly's ``/v3/streams/contents`` endpoint serves its cached copy of a public
feed without authentication. We use it only for sources configured with
``via: feedly`` or when a direct fetch is blocked (403/Cloudflare challenge).
Excerpt-only policy applies: summaries are stripped of HTML and truncated.
"""
from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import quote

from .util import clean_url, iso, make_excerpt, now_utc, wayback_url

FEEDLY_API = "https://cloud.feedly.com/v3/streams/contents"
FEEDLY_HOST = "cloud.feedly.com"
DEFAULT_COUNT = 50   # polite page size for routine collection
BACKFILL_COUNT = 100  # max page size used when paging history
MAX_BACKFILL_PAGES = 20


def stream_url(feed_url: str, count: int = DEFAULT_COUNT, continuation: str | None = None) -> str:
    url = f"{FEEDLY_API}?streamId={quote('feed/' + feed_url, safe='')}&count={int(count)}"
    if continuation:
        url += f"&continuation={quote(continuation, safe='')}"
    return url


def is_blocked_response(r) -> bool:
    """True for 403s and Cloudflare challenge pages (worth a Feedly fallback)."""
    if r is None:
        return False
    if r.status_code == 403:
        return True
    if r.status_code in (429, 503):
        if (r.headers.get("cf-mitigated") or "").lower() == "challenge":
            return True
        try:
            head = r.text[:4000]
        except Exception:
            return False
        return "Just a moment" in head or "challenge-platform" in head
    return False


def _item_url(item: dict) -> str | None:
    cands: list[str] = []
    # Order of preference: explicit canonical links, the alternate link, the
    # originId (often the permalink), then Feedly's canonicalUrl guess.
    for key in ("canonical", "alternate"):
        for link in item.get(key) or []:
            h = link.get("href") if isinstance(link, dict) else None
            if isinstance(h, str) and h.startswith("http"):
                cands.append(h)
    for key in ("originId", "canonicalUrl"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            cands.append(v)
    if not cands:
        return None
    # Skip #fragment links (they collapse onto an index page once cleaned) and
    # WordPress ?p=N shortlinks when a proper permalink is available.
    for u in cands:
        if "#" not in u and "?p=" not in u:
            return clean_url(u)
    return clean_url(cands[0])


def _item_text(item: dict) -> str:
    for k in ("summary", "content"):
        v = item.get(k)
        if isinstance(v, dict) and v.get("content"):
            return v["content"]
    return ""


def items_to_posts(items: list[dict], *, blog_id: str, via: str = "feedly") -> list[dict]:
    posts = []
    fetched = iso(now_utc())
    for it in items or []:
        url = _item_url(it)
        if not url:
            continue
        ts = it.get("published") or it.get("updated") or it.get("crawled")
        if not isinstance(ts, (int, float)) or ts <= 0:
            continue  # undated entries are static pages (e.g. "About"), not posts
        published = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        title = (it.get("title") or "").strip() or url
        tags = [k.strip() for k in (it.get("keywords") or []) if isinstance(k, str) and k.strip() and len(k) < 60]
        author = it.get("author")
        posts.append({
            "url": url,
            "title": title[:300],
            "blog": blog_id,
            "author": author.strip() if isinstance(author, str) and author.strip() else None,
            "published": iso(published),
            "fetched": fetched,
            "excerpt": make_excerpt(_item_text(it)),
            "tags": tags,
            "archive_url": wayback_url(url),
            "via": via,
        })
    return posts


async def fetch_page(client, gate, feed_url: str, *, count: int = DEFAULT_COUNT,
                     continuation: str | None = None) -> tuple[list[dict] | None, str | None, str | None]:
    """Fetch one page of Feedly's cache. Returns (items, continuation, error)."""
    from .http import get  # local import to avoid a cycle
    url = stream_url(feed_url, count=count, continuation=continuation)
    async with gate:
        await gate.wait_host(FEEDLY_HOST)
        r = await get(client, url, headers={"Accept": "application/json"}, browser_ua=True)
    if r is None:
        return None, None, "feedly: network error"
    if r.status_code != 200:
        return None, None, f"feedly: HTTP {r.status_code}"
    try:
        data = r.json()
    except Exception:
        return None, None, "feedly: bad json"
    items = data.get("items") or []
    return items, data.get("continuation"), None


def align_case(posts_map: dict[str, dict], post: dict) -> None:
    """Feedly sometimes caches a URL with different path casing than the site's
    own feed. Reuse the existing post's URL when keys match case-insensitively."""
    from .util import normalize_url
    key = normalize_url(post["url"])
    if key in posts_map:
        return
    low = key.lower()
    for k, existing in posts_map.items():
        if k.lower() == low:
            post["url"] = existing["url"]
            post["archive_url"] = existing.get("archive_url") or post["archive_url"]
            return
