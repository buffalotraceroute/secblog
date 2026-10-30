"""Deterministic (and optional LLM) tagging."""
from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml

from .sources import ROOT

CONFIG = ROOT / "config" / "taxonomy.yaml"


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def llm_tag(post: dict) -> dict | None:
    """Optional hook. Returns {category, tags} or None. Off unless SECBLOG_LLM_TAGGER=1."""
    if os.environ.get("SECBLOG_LLM_TAGGER", "").strip() not in ("1", "true", "yes"):
        return None
    # Users can replace this stub with a real LLM call. Never required for a run.
    return None


def tag_post(post: dict, blog_default: str | None = None) -> dict:
    """Mutate and return the post with category + tags filled in."""
    cfg = _cfg()
    llm = llm_tag(post)
    if llm:
        if llm.get("category"):
            post["category"] = llm["category"]
        if llm.get("tags"):
            post["tags"] = sorted(set((post.get("tags") or []) + list(llm["tags"])))
        return post

    url = post.get("url") or ""
    if "web.archive.org/web/" in url:
        import re as _re
        m = _re.search(r"web\.archive\.org/web/\d+/(https?://.*)", url)
        if m:
            url = m.group(1)
    url_tokens = url.replace("/", " ").replace("-", " ").replace("_", " ")
    text = " ".join(
        filter(None, [post.get("title"), post.get("excerpt"), " ".join(post.get("tags") or []), url_tokens])
    )
    tags: set[str] = set(post.get("tags") or [])

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

    if not post.get("category"):
        for rule in cfg.get("primary_rules", []):
            if any(re.search(p, text, re.I) for p in rule.get("any") or []):
                post["category"] = rule["category"]
                break
    if not post.get("category"):
        defaults = cfg.get("blog_defaults") or {}
        post["category"] = defaults.get(post.get("blog")) or blog_default or "News & Industry"

    post["tags"] = sorted(tags)
    return post
