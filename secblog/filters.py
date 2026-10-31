"""Shared "is this URL an article?" filter.

Every ingestion path (feeds, Feedly, WordPress API, paged feeds, sitemap and
Wayback backfill) funnels through ``store.merge_post`` / ``store.save_blog``,
which call :func:`reject_reason` so that taxonomy/listing pages, static site
pages, feeds and assets never get stored as posts.

Rules match on *path segments* (not substrings), so a real post slug such as
``/2024/05/tags-considered-harmful/`` or ``/blog/author-spoofing-in-git/`` is
kept, while ``/tags/``, ``/tags/rust/`` or ``/author/jane/`` are rejected.

Per-source extra rules: optional ``exclude_patterns`` (list of regexes) in
sources.yaml, matched with ``re.search`` against the URL path (plus ``?query``
when present) of the original (non-Wayback) URL.
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlsplit

# Taxonomy listing segments: rejected when followed by at most one more segment
# (the term), e.g. /tags/, /tags/rust/, /blog/category/web/, /author/jane/.
# Deeper paths (/blog/topics/threat-intelligence/<slug>/) are real articles on
# some sites and are kept.
TAXONOMY = {
    "tag", "tags", "category", "categories", "topic", "topics", "author", "authors",
    "series", "label", "labels", "keyword", "keywords", "people", "writer", "writers", "types",
}
# Archive segments: rejected when bare or followed only by date parts
# (/archive/, /archives/2019/, /archives/2019/05/). /archives/<slug> and
# Schneier-style /blog/archives/2006/07/post.html are kept.
ARCHIVE = {"archive", "archives"}
# Words that on their own make a listing/index page (/blog/, /en/posts/, /news/).
LISTING = {
    "blog", "blogs", "post", "posts", "articles", "article", "news", "research", "writeups",
    "writeup", "write-ups", "publications", "notes", "entries", "stories", "insights",
    "all", "latest", "recent", "index", "home", "homepage", "en", "en-us", "en_us",
    "en-gb", "de", "fr", "es", "it", "ja", "jp", "ko", "zh", "pt", "ru", "nl", "pl",
    "de-de", "fr-fr", "es-es", "ja-jp", "ko-kr", "zh-cn", "zh-tw", "pt-br",
}
# Section landing pages: never an article when the whole path is made of these
# (plus LISTING / language words), e.g. /talks/, /resources/papers/, /de/services/.
SECTION = {
    "advisories", "advisory", "security-advisories", "projects", "talks", "tools", "resources",
    "presentations", "papers", "whitepapers", "tutorials", "cves", "podcast", "podcasts", "events",
    "speaking", "conferences", "videos", "training", "courses", "services", "solutions", "products",
    "partners", "company", "customers", "industries", "cheatsheets", "ctf", "exploits", "pages",
    "tabs", "reports", "publications", "research", "talk", "curated",
}
LANG_CODES = {
    "en", "de", "fr", "es", "it", "ja", "jp", "ko", "zh", "pt", "ru", "nl", "pl", "ar", "hi", "fa",
    "tr", "sv", "da", "fi", "no", "cs", "he", "id", "th", "vi", "uk", "el", "hu", "ro",
}
REGION_CODES = {
    "us", "gb", "uk", "cn", "tw", "hk", "br", "de", "fr", "es", "jp", "kr", "au", "nz", "ca",
    "in", "sg", "mx", "it", "nl", "pl", "ru", "pt", "at", "ch", "be", "ie", "hans", "hant",
}
_LANG = re.compile(r"^([a-z]{2})(?:[-_]([a-z]{2,4}))?$")


def _is_lang(seg: str) -> bool:
    """'en', 'de-de', 'zh-cn', 'en_us' — but not slugs like 'hi-jack' or 'no-boot'."""
    m = _LANG.match(seg)
    return bool(m and m.group(1) in LANG_CODES and (m.group(2) is None or m.group(2) in REGION_CODES))


# Static, non-article pages (rejected when they are the last segment and every
# segment before them is a LISTING/STATIC/container word).
STATIC = {
    "about", "about-us", "about-me", "aboutme", "whoami", "me", "contact", "contacts", "contact-us",
    "contact-me", "privacy", "privacy-policy", "privacy-notice", "cookie-policy", "cookies",
    "terms", "terms-of-service", "terms-of-use", "terms-and-conditions", "legal",
    "imprint", "impressum", "disclaimer", "disclosure", "disclosure-policy",
    "responsible-disclosure", "responsible-disclosure-policy", "vulnerability-disclosure-policy",
    "security-policy", "home", "homepage", "index", "404", "404.html", "not-found", "search",
    "sitemap", "subscribe", "newsletter", "thank-you", "thanks", "signup", "sign-up", "login",
    "sign-in", "register", "sample-page", "careers", "jobs", "team", "faq",
    "archive", "archives", "tags", "categories", "authors", "series", "topics", "feed",
    "rss", "atom", "now", "uses", "links", "blogroll", "support", "donate", "sponsor",
}
CONTAINER = {"legal", "company", "pages", "page", "site", "info"}
FEED_NAMES = {"feed", "feeds", "rss", "atom", "index.xml", "feed.xml", "rss.xml", "atom.xml", "feed.json"}
ALWAYS_SEGMENTS = {
    "feed", "wp-content", "wp-includes", "wp-json", "wp-admin", "attachment", "cdn-cgi",
    "assets", "static", "images", "img", "uploads", "media", "files",
}
ASSET_EXT = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|avif|ico|bmp|tiff?|pdf|docx?|xlsx?|pptx?|zip|gz|tgz|7z|rar|"
    r"xml|rss|atom|json|txt|css|woff2?|ttf|eot|mp4|webm|mov|mp3|wav|ogg)$",
    re.I,
)
JUNK_QUERY = {
    "page", "paged", "s", "q", "search", "query", "tag", "cat", "category", "author",
    "updated-max", "updated-min", "max-results", "start-index", "by-date", "sort",
    "replytocom", "share", "attachment_id",
}
# Titles that by themselves mark a listing / static page.
JUNK_TITLES = {
    "tags", "tag", "categories", "category", "archives", "archive", "blog", "blogs", "posts",
    "all posts", "articles", "home", "homepage", "home page", "index", "search", "search results",
    "404", "page not found", "404 page not found", "not found", "authors", "series", "topics",
    "about", "about me", "about us", "contact", "contact us", "privacy policy", "rss.xml",
}
# Documents a feed itself publishes (e.g. Paged Out! issues as PDFs) are kept;
# the same file types found only via sitemap / Wayback crawling are assets.
DOC_EXT = re.compile(r"\.(pdf|docx?|pptx?)$", re.I)
FEED_VIAS = {"feed", "feed-paged", "feedly", "wp-api", "blogspot", "wayback-feed"}
ROOT_POST_QUERY = {"p", "post", "id", "article"}
_WAYBACK = re.compile(r"^https?://(?:web\.)?archive\.org/(?:web|wayback)/(?:\d+[a-z_]*/)?(.+)$", re.I)
_YEAR = re.compile(r"^(19|20)\d{2}$")
_SMALLNUM = re.compile(r"^\d{1,2}$")
_PAGEN = re.compile(r"^page-?\d+(\.html?)?$", re.I)


def unwrap(url: str) -> str:
    """Return the original URL for a Wayback snapshot URL (else the URL itself)."""
    m = _WAYBACK.match(url or "")
    if m:
        inner = m.group(1)
        if not inner.startswith("http"):
            inner = "https://" + inner
        return inner
    return url


def _segments(path: str) -> list[str]:
    return [unquote(s).lower() for s in path.split("/") if s]


def _strip_ext(seg: str) -> str:
    return re.sub(r"\.(html?|php|aspx?)$", "", seg)


def _date_parts_only(segs: list[str]) -> bool:
    """True for [], ['2019'], ['2019','05'], ['2019','05','17']."""
    if not segs:
        return True
    if not _YEAR.match(segs[0]):
        return False
    return len(segs) <= 3 and all(_SMALLNUM.match(s) for s in segs[1:])


def reject_reason(url: str, title: str | None = None, *, site_name: str | None = None,
                  exclude: list[str] | tuple[str, ...] = (), via: str | None = None) -> str | None:
    """Return why ``url`` is not an article, or None if it looks like one."""
    orig = unwrap(url)
    parts = urlsplit(orig)
    path = parts.path or "/"
    segs = _segments(path)
    target = path + (f"?{parts.query}" if parts.query else "")
    for pat in exclude or ():
        if re.search(pat, target, re.I):
            return f"exclude_pattern:{pat}"

    keys: set[str] = set()
    if parts.query:
        keys = {k.lower() for k, _ in parse_qsl(parts.query, keep_blank_values=True)}
        bad = keys & JUNK_QUERY
        if bad:
            return f"query:{sorted(bad)[0]}"

    if not segs:
        # The bare homepage; WordPress ?p=123 shortlinks are real posts.
        return None if keys & ROOT_POST_QUERY else "root"
    last = segs[-1]
    if last in FEED_NAMES:
        return "feed"
    if ASSET_EXT.search(last):
        if not (DOC_EXT.search(last) and via in FEED_VIAS):
            return "asset"
    for s in segs:
        if s in ALWAYS_SEGMENTS:
            return f"segment:{s}"
    for s in segs[:-1]:
        if s.endswith("_files"):  # "Save page as" asset folders
            return "segment:_files"

    # Pagination: /page/2/, /blog/page2/, /tags/x/page/3
    for i, s in enumerate(segs):
        if s == "page" and i + 1 < len(segs) and segs[i + 1].isdigit():
            return "pagination"
        if _PAGEN.match(s):
            return "pagination"

    for i, s in enumerate(segs):
        rest = segs[i + 1:]
        if s in TAXONOMY and len(rest) <= 1:
            return f"taxonomy:{s}"
        if s in ARCHIVE and _date_parts_only(rest):
            return f"archive:{s}"
        if s == "search" and len(rest) <= 2:
            return "search"

    stripped = [_strip_ext(s) for s in segs]
    # Listing index / date archive: every segment is a listing word or date part.
    non_listing = [s for s in stripped
                   if s not in LISTING and s not in SECTION and s not in CONTAINER and not _is_lang(s)]
    if not non_listing or _date_parts_only(non_listing):
        return "listing-index"
    # Static pages: /about/, /blog/contact/, /en/legal/privacy-policy.html
    if stripped[-1] in STATIC and all(s in LISTING or s in STATIC or s in CONTAINER or s in SECTION or _is_lang(s)
                                       for s in stripped[:-1]):
        return f"static:{stripped[-1]}"

    t = re.sub(r"\s+", " ", (title or "")).strip().lower()
    if t:
        if t in JUNK_TITLES and len(segs) <= 2:
            return f"title:{t}"
        if site_name and t == site_name.strip().lower() and len(segs) <= 1:
            return "title:site-name"
    return None


@lru_cache(maxsize=1)
def _source_rules() -> dict[str, tuple[str, tuple[str, ...]]]:
    from .sources import load_sources
    try:
        return {s.id: (s.name, tuple(s.exclude_patterns or ())) for s in load_sources()}
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _source_bases() -> dict[str, str]:
    from .sources import load_sources
    from .util import site_base
    try:
        return {s.id: site_base(s.homepage) for s in load_sources()}
    except Exception:
        return {}


def fix_post_urls(post: dict, blog_id: str | None = None, base: str | None = None) -> bool:
    """Rebase a post's url / archive_url when they point at a dev server
    (0.0.0.0, localhost, 127.x...) or have no host. Returns True if changed."""
    from .util import rehost_url
    base = base or _source_bases().get(blog_id or post.get("blog") or "")
    if not base:
        return False
    changed = False
    url = post.get("url") or ""
    new = rehost_url(url, base)
    if new != url:
        post["url"] = new
        changed = True
    arch = post.get("archive_url") or ""
    m = re.match(r"^(https?://(?:web\.)?archive\.org/web/(?:\d+[a-z_]*/)?)(.*)$", arch)
    if m and m.group(2):
        inner = rehost_url(m.group(2), base)
        if inner != m.group(2):
            post["archive_url"] = m.group(1) + inner
            changed = True
    return changed


def post_reject_reason(post: dict, blog_id: str | None = None) -> str | None:
    """reject_reason for a post dict, using its source's name / exclude_patterns."""
    name, exclude = _source_rules().get(blog_id or post.get("blog") or "", (None, ()))
    return reject_reason(post.get("url") or "", post.get("title"), site_name=name, exclude=exclude,
                         via=post.get("via"))


def is_article(post: dict, blog_id: str | None = None) -> bool:
    return post_reject_reason(post, blog_id) is None
