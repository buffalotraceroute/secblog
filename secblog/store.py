"""Git-friendly post storage: one JSONL file per blog under data/posts/.

Each line is one post, keys sorted, posts sorted newest first then by URL, so
diffs stay small and stable. Only metadata and a short excerpt are stored.
"""
from __future__ import annotations

import json
from pathlib import Path

from .sources import ROOT
from .util import EXCERPT_MAX, normalize_url, post_id

POSTS_DIR = ROOT / "data" / "posts"
STATE_DIR = ROOT / "data" / "state"

FIELDS = [
    "id", "url", "title", "blog", "author", "published", "fetched",
    "excerpt", "tags", "category", "category_source", "archive_url", "via",
]


def _sort_key(p: dict):
    return (p.get("published") or "0000", p["url"])


def load_blog(blog_id: str) -> dict[str, dict]:
    path = POSTS_DIR / f"{blog_id}.jsonl"
    posts: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                p = json.loads(line)
                posts[normalize_url(p["url"])] = p
    return posts


def save_blog(blog_id: str, posts: dict[str, dict]) -> None:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTS_DIR / f"{blog_id}.jsonl"
    if not posts:
        if path.exists():
            path.unlink()
        return
    rows = sorted(posts.values(), key=_sort_key, reverse=True)
    text = "".join(json.dumps(_clean(p), ensure_ascii=False, sort_keys=True) + "\n" for p in rows)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")


def _clean(p: dict) -> dict:
    out = {k: p.get(k) for k in FIELDS}
    out["id"] = out["id"] or post_id(out["url"])
    out["tags"] = sorted(set(out.get("tags") or []))
    if out.get("excerpt") and len(out["excerpt"]) > EXCERPT_MAX + 1:
        out["excerpt"] = out["excerpt"][:EXCERPT_MAX] + "…"
    return {k: v for k, v in out.items() if v not in (None, "", [])} | {"tags": out["tags"]}


def merge_post(posts: dict[str, dict], new: dict) -> bool:
    """Insert or update a post. Returns True if anything changed.

    First-seen `fetched` timestamp is preserved; feed data (title/excerpt/date)
    wins over lower-quality sources such as sitemap- or URL-derived titles.
    """
    key = normalize_url(new["url"])
    old = posts.get(key)
    if old is None:
        new.setdefault("id", post_id(new["url"]))
        posts[key] = new
        return True
    quality = {"feed": 3, "feedly": 2, "wp-api": 3, "blogspot": 3, "feed-paged": 3, "wayback-feed": 2, "html-meta": 2, "sitemap": 1, "wayback-cdx": 0}
    changed = False
    better = quality.get(new.get("via"), 1) >= quality.get(old.get("via"), 1)
    for k in ("title", "excerpt", "author", "published", "archive_url"):
        v = new.get(k)
        if not v:
            continue
        if not old.get(k) or (better and old.get(k) != v):
            old[k] = v
            changed = True
    if better and new.get("via") and old.get("via") != new.get("via"):
        old["via"] = new["via"]
        changed = True
    return changed


def iter_all_posts():
    for path in sorted(POSTS_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def load_json(name: str) -> dict:
    path = STATE_DIR / name
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(name: str, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / name).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
