"""Shared helpers: URL normalisation, HTML stripping, excerpts, dates."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dateutil import parser as dateparser

USER_AGENT = (
    "Mozilla/5.0 (compatible; secblog-archiver/1.0; "
    "+https://buffalotraceroute.github.io/secblog/about/)"
)
# Some CDNs reject anything that does not look like a browser. The collector
# retries once with this UA when it gets a 403/429 from a feed.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Safari/537.36 secblog-archiver/1.0"
)

EXCERPT_MAX = 300

_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z_]+|fbclid|gclid|mc_cid|mc_eid|ref|ref_src|source|sk|"
    r"_hsenc|_hsmi|hsCtaTracking|mkt_tok|amp|s|share|feed|rss|wt\.mc_id)$",
    re.I,
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_date(value) -> datetime | None:
    """Parse a date string / struct_time into an aware UTC datetime (or None)."""
    if not value:
        return None
    try:
        if hasattr(value, "tm_year"):
            dt = datetime(*value[:6], tzinfo=timezone.utc)
        elif isinstance(value, datetime):
            dt = value
        else:
            dt = dateparser.parse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    # Reject obviously bogus dates (feeds sometimes emit 1970 or far future).
    if dt.year < 1995 or dt > now_utc().replace(year=now_utc().year + 1):
        return None
    return dt


def force_https(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


def clean_url(url: str) -> str:
    """Return a cleaned, linkable canonical URL (https, no tracking params, no fragment)."""
    url = (url or "").strip()
    if not url:
        return url
    url = force_https(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING_PARAMS.match(k)]
    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return urlunsplit(("https", host, path, urlencode(query), ""))


def normalize_url(url: str) -> str:
    """Dedupe key: cleaned URL, scheme-less, without www., trailing slash or index pages."""
    u = clean_url(url)
    parts = urlsplit(u)
    host = parts.netloc
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/(index|default)\.(html?|php|aspx?)$", "/", parts.path)
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    # Medium appends a random hash to slugs; the hash alone identifies the post.
    if host.endswith("medium.com"):
        m = re.search(r"-([0-9a-f]{10,12})$", path)
        if m:
            path = "/p/" + m.group(1)
    q = "&".join(sorted(parts.query.split("&"))) if parts.query else ""
    return f"{host}{path}" + (f"?{q}" if q else "")


def post_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def wayback_url(url: str, timestamp: str | None = None) -> str:
    return f"https://web.archive.org/web/{timestamp or ''}{'/' if timestamp else ''}{url}"


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_DROP_BLOCKS = re.compile(r"<(script|style|noscript|iframe|svg|figure|pre|code)\b.*?</\1>", re.S | re.I)


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = _DROP_BLOCKS.sub(" ", text)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>", " ", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    return _WS_RE.sub(" ", text).strip()


_BOILERPLATE = re.compile(
    r"(The post .{0,200}? appeared first on .*$|Continue reading.*$|Read more.*$|\[(…|\.\.\.)\]\s*$)", re.I
)


def make_excerpt(text: str | None, limit: int = EXCERPT_MAX) -> str:
    """Plain-text excerpt, at most `limit` characters, cut at a word boundary."""
    plain = strip_html(text)
    plain = _BOILERPLATE.sub("", plain).strip()
    if len(plain) <= limit:
        return plain
    cut = plain[: limit - 1]
    if " " in cut[int(limit * 0.6):]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,.;:-") + "…"


def slugify(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("+", "p").replace("#", "sharp")
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-") or "x"


def title_from_url(url: str) -> str:
    """Fallback title derived from the URL slug (used for sitemap / Wayback-only posts)."""
    path = urlsplit(url).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else urlsplit(url).netloc
    slug = re.sub(r"\.(html?|php|aspx?)$", "", slug)
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", slug)
    words = re.split(r"[-_+]+", slug)
    words = [w for w in words if w]
    if not words:
        return url
    return " ".join(w if w.isupper() or any(c.isdigit() for c in w) else w.capitalize() for w in words)
