"""Collect sites that have no RSS/Atom feed by reading their post listings.

Configured per source in sources.yaml:

    listing:
      type: gatsby            # Gatsby page-data JSON
      # url: https://x/       # site base (default: homepage)
      # max_pages: 100

    listing:
      type: html              # plain HTML index pages
      url: https://x/posts/   # first listing page (default: homepage)
      post_pattern: "^/posts/[^/]+/?$"   # regex on the URL path of post links
      # page_pattern: "/page/\\d+/?$"    # regex for pagination links
      # max_pages: 50
      # fetch_meta: true                 # pull title/date/excerpt from each post page

The regular 6-hourly collection reads the first listing page(s) only and
fetches details just for posts it has not seen; backfill walks every page.
Only metadata plus a short excerpt is stored, never full text. Non-article
links (/whoami/, tags, about...) are dropped by the shared filter in
store.merge_post.
"""
from __future__ import annotations

import json
import re
from html import unescape
from urllib.parse import urljoin, urlsplit

from .http import Gate, get
from .util import clean_url, iso, make_excerpt, normalize_url, now_utc, parse_date, strip_html, wayback_url

COLLECT_PAGES = 2       # pages read by the regular collector
DETAIL_ON_COLLECT = 8   # new posts to enrich with per-page meta during collect
DEFAULT_MAX_PAGES = 100
_LINK_RE = re.compile(r"""<a\b[^>]*\bhref=["']([^"'#]+)["'][^>]*>""", re.I)
_NEXT_RE = re.compile(r"""<a\b[^>]*\brel=["'][^"']*\bnext\b[^"']*["'][^>]*\bhref=["']([^"']+)["']""", re.I)
_META_RE = re.compile(
    r"""<meta\b[^>]*(?:name|property)=["']([^"']+)["'][^>]*content=["']([^"']*)["']"""
    r"""|<meta\b[^>]*content=["']([^"']*)["'][^>]*(?:name|property)=["']([^"']+)["']""",
    re.I,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TIME_RE = re.compile(r"""<time\b[^>]*(?:datetime=["']([^"']+)["'])?[^>]*>(.*?)</time>""", re.I | re.S)


def _base(src) -> str:
    cfg = src.listing or {}
    return (cfg.get("url") or src.homepage or "").rstrip("/") + "/"


def _max_pages(src, *, backfill: bool) -> int:
    cfg = src.listing or {}
    if not backfill:
        return min(int(cfg.get("collect_pages") or COLLECT_PAGES), int(cfg.get("max_pages") or DEFAULT_MAX_PAGES))
    return int(cfg.get("max_pages") or DEFAULT_MAX_PAGES)


def _post(blog_id: str, url: str, *, title: str | None, published, excerpt: str | None, via: str) -> dict:
    return {
        "url": clean_url(url),
        "title": (title or url)[:300],
        "blog": blog_id,
        "author": None,
        "published": iso(published) if published else None,
        "fetched": iso(now_utc()),
        "excerpt": make_excerpt(excerpt or ""),
        "tags": [],
        "archive_url": wayback_url(clean_url(url)),
        "via": via,
    }


# ---------- Gatsby ----------------------------------------------------------

def _gatsby_page_url(base: str, index: int) -> str:
    # Gatsby: page 1 is /page-data/index/page-data.json, then /page-data/2/...
    path = "index" if index <= 1 else str(index)
    return urljoin(base, f"page-data/{path}/page-data.json")


def _gatsby_nodes(data: dict) -> list[dict]:
    """Pull post nodes out of a Gatsby page-data payload (index or blog-post)."""
    result = (data or {}).get("result") or {}
    # Index / listing pages: pageContext.group = [{node: {fields, frontmatter, excerpt}}]
    group = (result.get("pageContext") or {}).get("group") or []
    nodes = []
    for item in group:
        node = item.get("node") if isinstance(item, dict) else None
        if isinstance(node, dict):
            nodes.append(node)
    # Single post page: result.data.markdownRemark / mdx / ...
    data_block = result.get("data") or {}
    for key in ("markdownRemark", "mdx", "post", "blogPost"):
        node = data_block.get(key)
        if isinstance(node, dict):
            nodes.append(node)
    return nodes


def _gatsby_node_to_post(node: dict, blog_id: str, base: str) -> dict | None:
    fields = node.get("fields") or {}
    fm = node.get("frontmatter") or {}
    slug = fields.get("slug") or node.get("slug") or fm.get("slug") or ""
    if not slug and node.get("path"):
        slug = node["path"]
    if not slug:
        return None
    url = urljoin(base, slug if slug.startswith("/") else "/" + slug)
    title = (fm.get("title") or node.get("title") or "").strip()
    date = parse_date(fm.get("date") or fm.get("published") or node.get("date"))
    excerpt = node.get("excerpt") or fm.get("description") or fm.get("excerpt") or ""
    # Prefer the short <!-- end --> / description; never store html body.
    if node.get("html") and not excerpt:
        excerpt = make_excerpt(node["html"])
    return _post(blog_id, url, title=title or None, published=date, excerpt=excerpt, via="gatsby")


async def _fetch_json(client, gate, url: str) -> dict | None:
    async with gate:
        await gate.wait_host(urlsplit(url).hostname or "")
        r = await get(client, url, browser_ua=True, headers={"Accept": "application/json"})
    if r is None or r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        try:
            return json.loads(r.content)
        except Exception:
            return None


async def collect_gatsby(client, gate, src, *, backfill: bool = False) -> tuple[list[dict], str | None]:
    base = _base(src)
    max_pages = _max_pages(src, backfill=backfill)
    posts: list[dict] = []
    seen: set[str] = set()
    page_count = None
    for i in range(1, max_pages + 1):
        data = await _fetch_json(client, gate, _gatsby_page_url(base, i))
        if data is None:
            if i == 1:
                return [], f"gatsby: no page-data at {base}"
            break
        ctx = (data.get("result") or {}).get("pageContext") or {}
        if page_count is None and ctx.get("pageCount"):
            page_count = int(ctx["pageCount"])
        for node in _gatsby_nodes(data):
            p = _gatsby_node_to_post(node, src.id, base)
            if not p:
                continue
            key = normalize_url(p["url"])
            if key in seen:
                continue
            seen.add(key)
            posts.append(p)
        if page_count and i >= page_count:
            break
        if ctx.get("last") is True:
            break
    return posts, None


# ---------- HTML listing ----------------------------------------------------

def _html_links(html: str, page_url: str) -> list[str]:
    out = []
    for m in _LINK_RE.finditer(html or ""):
        href = unescape(m.group(1).strip())
        if href.startswith(("mailto:", "javascript:", "data:", "#")):
            continue
        out.append(clean_url(urljoin(page_url, href)))
    return out


def _html_next(html: str, page_url: str, page_pattern: re.Pattern | None, index: int) -> str | None:
    m = _NEXT_RE.search(html or "")
    if m:
        return clean_url(urljoin(page_url, unescape(m.group(1))))
    if page_pattern:
        for link in _html_links(html, page_url):
            if page_pattern.search(urlsplit(link).path or ""):
                # Prefer the next page number when possible.
                if re.search(rf"/(?:page/)?{index + 1}/?$", urlsplit(link).path or ""):
                    return link
        # Fall through: try constructing /N/ or /page/N/
    parts = urlsplit(page_url)
    for cand in (f"{index + 1}/", f"page/{index + 1}/", f"page/{index + 1}"):
        guess = urljoin(page_url if page_url.endswith("/") else page_url + "/", cand)
        # Only return a constructed guess when the caller will validate by fetching.
        if index == 1 and parts.path in ("/", ""):
            return clean_url(urljoin(f"{parts.scheme}://{parts.netloc}/", f"{index + 1}/"))
    return None


def _meta_map(html: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in _META_RE.finditer(html or ""):
        if m.group(1):
            out[m.group(1).lower()] = unescape(m.group(2) or "")
        else:
            out[m.group(4).lower()] = unescape(m.group(3) or "")
    return out


def _detail_from_html(html: str, url: str) -> dict:
    meta = _meta_map(html)
    title = (meta.get("og:title") or meta.get("twitter:title") or "").strip()
    if not title:
        m = _TITLE_RE.search(html or "")
        title = strip_html(m.group(1)).strip() if m else ""
    title = re.sub(r"\s+[|\u2013\u2014].*$", "", title).strip()
    excerpt = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""
    published = parse_date(
        meta.get("article:published_time") or meta.get("datePublished") or meta.get("publishdate")
        or meta.get("date") or meta.get("dc.date")
    )
    if published is None:
        for m in _TIME_RE.finditer(html or ""):
            published = parse_date(m.group(1) or strip_html(m.group(2)))
            if published:
                break
    return {"title": title or None, "excerpt": excerpt, "published": published}


async def _fetch_html(client, gate, url: str) -> str | None:
    async with gate:
        await gate.wait_host(urlsplit(url).hostname or "")
        r = await get(client, url, browser_ua=True, headers={"Accept": "text/html,*/*;q=0.8"})
    if r is None or r.status_code != 200:
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if "html" not in ctype and not (r.content[:200].lstrip().lower().startswith((b"<!doctype", b"<html"))):
        return None
    return r.text


async def collect_html(client, gate, src, *, backfill: bool = False) -> tuple[list[dict], str | None]:
    cfg = src.listing or {}
    start = cfg.get("url") or src.homepage
    if not start:
        return [], "html-listing: no url"
    post_re = re.compile(cfg["post_pattern"]) if cfg.get("post_pattern") else None
    page_re = re.compile(cfg["page_pattern"]) if cfg.get("page_pattern") else None
    fetch_meta = bool(cfg.get("fetch_meta", True))
    max_pages = _max_pages(src, backfill=backfill)
    posts: list[dict] = []
    seen: set[str] = set()
    page_url = clean_url(start)
    for i in range(1, max_pages + 1):
        html = await _fetch_html(client, gate, page_url)
        if html is None:
            if i == 1:
                return [], f"html-listing: HTTP error on {page_url}"
            break
        for link in _html_links(html, page_url):
            path = urlsplit(link).path or "/"
            if post_re and not post_re.search(path):
                continue
            if not post_re:
                # Without a pattern keep only same-host links that look like a slug page.
                if urlsplit(link).hostname != urlsplit(page_url).hostname:
                    continue
                segs = [s for s in path.split("/") if s]
                if len(segs) < 1 or path.rstrip("/").endswith(("/page", "/posts", "/blog", "/tags")):
                    continue
            key = normalize_url(link)
            if key in seen:
                continue
            seen.add(key)
            posts.append(_post(src.id, link, title=None, published=None, excerpt=None, via="html-listing"))
        nxt = _html_next(html, page_url, page_re, i)
        if not nxt or normalize_url(nxt) == normalize_url(page_url):
            # Try /2/, /3/ style when still on page 1 of a root listing.
            if i == 1 and not page_re:
                guess = clean_url(urljoin(_base(src), f"{i + 1}/"))
                if normalize_url(guess) != normalize_url(page_url):
                    nxt = guess
                else:
                    break
            else:
                break
        page_url = nxt

    if fetch_meta:
        # Enrich with per-post meta. During collect, only fill blanks for posts
        # that still need a title (caller may further limit); during backfill do all.
        for p in posts:
            html = await _fetch_html(client, gate, p["url"])
            if not html:
                continue
            d = _detail_from_html(html, p["url"])
            if d["title"]:
                p["title"] = d["title"][:300]
            if d["excerpt"]:
                p["excerpt"] = make_excerpt(d["excerpt"])
            if d["published"]:
                p["published"] = iso(d["published"])
    return posts, None


# ---------- Entry points ----------------------------------------------------

async def collect_listing(client, gate, src, *, backfill: bool = False) -> tuple[list[dict], str | None]:
    """Return (posts, error) for a source with a ``listing:`` block."""
    cfg = src.listing or {}
    kind = (cfg.get("type") or "").lower()
    if kind == "gatsby":
        return await collect_gatsby(client, gate, src, backfill=backfill)
    if kind == "html":
        return await collect_html(client, gate, src, backfill=backfill)
    return [], f"listing: unknown type {kind!r}"
