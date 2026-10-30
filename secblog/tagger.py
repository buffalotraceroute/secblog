"""Deterministic (and optional LLM) tagging.

Category assignment:
  1. Keyword / taxonomy rules → category_source: keyword
  2. Else explicit sources.yaml default_category (or legacy category) → blog_default
  3. Else blog's dominant keyword-matched category (ignoring News & Industry) → blog_default
  4. Else News & Industry (genuine news/aggregator blogs, or unknown) → blog_default
"""
from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path

import yaml

from .sources import ROOT, Source, load_sources

CONFIG = ROOT / "config" / "taxonomy.yaml"
NEWS_FALLBACK = "News & Industry"


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def clear_cfg_cache() -> None:
    _cfg.cache_clear()


def llm_tag(post: dict) -> dict | None:
    """Optional hook. Returns {category, tags} or None. Off unless SECBLOG_LLM_TAGGER=1."""
    if os.environ.get("SECBLOG_LLM_TAGGER", "").strip() not in ("1", "true", "yes"):
        return None
    return None


def _match_text(post: dict) -> str:
    url = post.get("url") or ""
    if "web.archive.org/web/" in url:
        m = re.search(r"web\.archive\.org/web/\d+/(https?://.*)", url)
        if m:
            url = m.group(1)
    url_tokens = url.replace("/", " ").replace("-", " ").replace("_", " ")
    return " ".join(
        filter(None, [post.get("title"), post.get("excerpt"), " ".join(post.get("tags") or []), url_tokens])
    )


def extract_tags(text: str, existing: list[str] | None = None) -> list[str]:
    cfg = _cfg()
    tags: set[str] = set(existing or [])
    for rule in cfg.get("tag_rules", []):
        if "pattern" in rule:
            flags = 0 if rule.get("normalize") == "upper" else re.I
            for m in re.finditer(rule["pattern"], text, flags):
                val = m.group(1) if m.lastindex else m.group(0)
                if rule.get("normalize") == "upper":
                    val = val.upper()
                tags.add(val)
        for name in rule.get("any_of") or []:
            if re.search(r"\b" + re.escape(name) + r"\b", text, re.I):
                tags.add(name)
    return sorted(tags)


def keyword_category(text: str) -> str | None:
    """Return the first matching primary category, or None if no rule hits."""
    cfg = _cfg()
    for rule in cfg.get("primary_rules", []):
        if any(re.search(p, text, re.I) for p in rule.get("any") or []):
            return rule["category"]
    return None


def news_blog_ids() -> set[str]:
    cfg = _cfg()
    return set(cfg.get("news_blogs") or [])


def explicit_default(src: Source | None, blog_id: str | None) -> str | None:
    """Per-blog override from sources.yaml or taxonomy blog_defaults."""
    cfg = _cfg()
    if src is not None:
        # Prefer dedicated default_category; fall back to legacy `category` field.
        val = getattr(src, "default_category", None) or src.category
        if val:
            return val
    defaults = cfg.get("blog_defaults") or {}
    if blog_id and blog_id in defaults:
        return defaults[blog_id]
    return None


def compute_blog_defaults(
    posts_by_blog: dict[str, list[dict]],
    sources: dict[str, Source] | None = None,
) -> dict[str, str]:
    """Dominant keyword category per blog (ignoring News & Industry), with overrides."""
    sources = sources or {s.id: s for s in load_sources()}
    news = news_blog_ids()
    out: dict[str, str] = {}

    for blog_id, posts in posts_by_blog.items():
        src = sources.get(blog_id)
        explicit = explicit_default(src, blog_id)
        if explicit:
            out[blog_id] = explicit
            continue
        if blog_id in news:
            out[blog_id] = NEWS_FALLBACK
            continue
        counts: Counter[str] = Counter()
        for p in posts:
            text = _match_text(p)
            cat = keyword_category(text)
            if cat and cat != NEWS_FALLBACK:
                counts[cat] += 1
        if counts:
            out[blog_id] = counts.most_common(1)[0][0]
        else:
            out[blog_id] = NEWS_FALLBACK
    return out


def tag_post(
    post: dict,
    blog_default: str | None = None,
    *,
    force: bool = True,
) -> dict:
    """Mutate and return the post with category, category_source, and tags.

    If force=True (default), recompute category from keywords / blog default even
    when a category is already present. Tags are always refreshed.
    """
    cfg = _cfg()
    llm = llm_tag(post)
    if llm:
        if llm.get("category"):
            post["category"] = llm["category"]
            post["category_source"] = "llm"
        if llm.get("tags"):
            post["tags"] = sorted(set((post.get("tags") or []) + list(llm["tags"])))
        return post

    text = _match_text(post)
    # Preserve non-taxonomy tags? We re-extract from text + keep prior freeform that
    # look like feed categories only if they aren't already covered — keep it simple:
    # merge feed tags that aren't CVE/tool noise by re-adding previous then extracting.
    prior = [t for t in (post.get("tags") or []) if not t.startswith("wayback")]
    post["tags"] = extract_tags(text, prior)

    kw = keyword_category(text)
    if kw:
        post["category"] = kw
        post["category_source"] = "keyword"
        return post

    # No keyword hit → blog default chain
    default = blog_default
    if not default:
        defaults = cfg.get("blog_defaults") or {}
        default = defaults.get(post.get("blog")) or NEWS_FALLBACK
    post["category"] = default
    post["category_source"] = "blog_default"
    return post


def retag_all() -> dict:
    """Recompute tags/categories for every stored post. Returns summary stats."""
    clear_cfg_cache()
    from . import store

    sources = {s.id: s for s in load_sources()}
    # Load everything first so dominant categories see the full keyword set.
    by_blog: dict[str, dict[str, dict]] = {}
    lists: dict[str, list[dict]] = defaultdict(list)
    for src in sources:
        by_blog[src] = store.load_blog(src)
        lists[src] = list(by_blog[src].values())
    # Also include blogs that exist on disk but not in sources (shouldn't happen)
    for path in store.POSTS_DIR.glob("*.jsonl"):
        if path.stem not in by_blog:
            by_blog[path.stem] = store.load_blog(path.stem)
            lists[path.stem] = list(by_blog[path.stem].values())

    defaults = compute_blog_defaults(lists, sources)
    changed = 0
    total = 0
    sources_used = Counter()
    cats = Counter()
    for blog_id, posts_map in by_blog.items():
        blog_changed = False
        default = defaults.get(blog_id, NEWS_FALLBACK)
        for p in posts_map.values():
            total += 1
            old = (p.get("category"), p.get("category_source"), tuple(p.get("tags") or []))
            tag_post(p, blog_default=default, force=True)
            new = (p.get("category"), p.get("category_source"), tuple(p.get("tags") or []))
            if old != new:
                blog_changed = True
                changed += 1
            sources_used[p.get("category_source") or "?"] += 1
            cats[p.get("category") or "?"] += 1
        if blog_changed or posts_map:
            store.save_blog(blog_id, posts_map)
    return {
        "total": total,
        "changed": changed,
        "category_source": dict(sources_used),
        "categories": dict(cats.most_common()),
        "blog_defaults": defaults,
    }
