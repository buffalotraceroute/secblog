"""Parse RSS/Atom and extract post metadata."""
from __future__ import annotations

import feedparser

from .util import clean_url, iso, make_excerpt, now_utc, parse_date, wayback_url


def entry_url(entry) -> str | None:
    for key in ("link", "id", "guid"):
        v = entry.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return clean_url(v)
        if hasattr(v, "href") and v.href:
            return clean_url(v.href)
    for l in entry.get("links") or []:
        if l.get("rel") in (None, "alternate") and l.get("href", "").startswith("http"):
            return clean_url(l["href"])
    return None


def entry_content(entry) -> str:
    if entry.get("summary"):
        return entry.summary
    for c in entry.get("content") or []:
        if c.get("value"):
            return c["value"]
    return entry.get("description") or ""


def entry_author(entry) -> str | None:
    if entry.get("author"):
        return entry.author.strip()
    for a in entry.get("authors") or []:
        if a.get("name"):
            return a["name"].strip()
    return None


def entry_tags(entry) -> list[str]:
    tags = []
    for t in entry.get("tags") or []:
        term = (t.get("term") or t.get("label") or "").strip()
        if term and len(term) < 60:
            tags.append(term)
    return tags


def parse_feed(content: bytes | str, *, blog_id: str, via: str = "feed", archive: bool = False) -> list[dict]:
    d = feedparser.parse(content)
    posts = []
    fetched = iso(now_utc())
    for e in d.entries:
        url = entry_url(e)
        if not url:
            continue
        published = parse_date(e.get("published") or e.get("updated") or e.get("created")
                               or (e.get("published_parsed") or e.get("updated_parsed")))
        title = (e.get("title") or "").strip() or url
        post = {
            "url": url,
            "title": title[:300],
            "blog": blog_id,
            "author": entry_author(e),
            "published": iso(published) if published else None,
            "fetched": fetched,
            "excerpt": make_excerpt(entry_content(e)),
            "tags": entry_tags(e),
            "archive_url": wayback_url(url),
            "via": via,
        }
        if archive:
            # Prefer a Wayback capture of the feed URL itself when we are ingesting
            # from a snapshot; individual posts still get a generic archive link.
            pass
        posts.append(post)
    return posts
