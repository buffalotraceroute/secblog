"""Python static site generator for secblog (Jinja2 + Pagefind)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import store
from .sources import STATUS_LABELS, load_sources
from .util import slugify

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
DIST = ROOT / "dist"
BASE_PATH = "/secblog"
PER_PAGE = 100
# Cap individual tag pages written (still listed on /tags/ for the top N).
MAX_TAG_PAGES = 800


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["base"] = BASE_PATH
    env.globals["site_name"] = "secblog"
    env.globals["site_url"] = f"https://buffalotraceroute.github.io{BASE_PATH}"
    env.globals["slugify"] = slugify
    env.globals["status_labels"] = STATUS_LABELS

    def ymd(s):
        return (s or "")[:10]

    def year(s):
        return (s or "")[:4]

    env.filters["ymd"] = ymd
    env.filters["year"] = year
    return env


def load_all():
    sources = {s.id: s for s in load_sources()}
    posts = list(store.iter_all_posts())
    posts.sort(key=lambda p: (p.get("published") or "0000", p["url"]), reverse=True)
    return sources, posts


def _write_paged(env, dist_rel_prefix: str, template: str, posts: list, sources, *, title: str, heading: str, kind: str, nav: str, extra: dict | None = None):
    """Write index + page/N for a post list. dist_rel_prefix like 'category/web-appsec' (no trailing slash)."""
    pages = max(1, (len(posts) + PER_PAGE - 1) // PER_PAGE)
    extra = extra or {}
    for i in range(pages):
        chunk = posts[i * PER_PAGE : (i + 1) * PER_PAGE]
        if i == 0:
            rel = f"{dist_rel_prefix}/index.html" if dist_rel_prefix else "index.html"
        else:
            rel = f"{dist_rel_prefix}/page/{i + 1}/index.html" if dist_rel_prefix else f"page/{i + 1}/index.html"
        path = DIST / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx = dict(
            title=title if i == 0 else f"{title} — page {i + 1}",
            heading=heading,
            kind=kind,
            posts=chunk,
            total=len(posts),
            page=i + 1,
            pages=pages,
            sources=sources,
            nav=nav,
            list_base=f"{BASE_PATH}/{dist_rel_prefix}" if dist_rel_prefix else BASE_PATH,
            **extra,
        )
        path.write_text(env.get_template(template).render(**ctx), encoding="utf-8")


def build(pagefind: bool = True) -> dict:
    sources, posts = load_all()
    env = _env()
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    shutil.copytree(STATIC_DIR, DIST / "assets")

    by_cat: dict[str, list] = defaultdict(list)
    by_tag: dict[str, list] = defaultdict(list)
    by_blog: dict[str, list] = defaultdict(list)
    by_year: dict[str, list] = defaultdict(list)
    for p in posts:
        by_cat[p.get("category") or "News & Industry"].append(p)
        by_blog[p["blog"]].append(p)
        y = (p.get("published") or "")[:4]
        if y:
            by_year[y].append(p)
        for t in p.get("tags") or []:
            by_tag[t].append(p)

    def write(rel: str, template: str, **ctx):
        path = DIST / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(env.get_template(template).render(**ctx), encoding="utf-8")

    # Home (paginated)
    _write_paged(
        env, "", "index.html", posts, sources,
        title="Latest", heading="Latest", kind="latest", nav="home",
        extra={"categories": sorted(by_cat.keys()), "years": sorted(by_year.keys(), reverse=True)},
    )

    for cat, plist in by_cat.items():
        _write_paged(
            env, f"category/{slugify(cat)}", "list.html", plist, sources,
            title=cat, heading=cat, kind="category", nav="categories",
        )

    # Tags: write pages for the most common tags only (keeps build tractable)
    top_tags = sorted(by_tag.items(), key=lambda x: -len(x[1]))[:MAX_TAG_PAGES]
    for tag, plist in top_tags:
        _write_paged(
            env, f"tag/{slugify(tag)}", "list.html", plist, sources,
            title=f"Tag: {tag}", heading=f"#{tag}", kind="tag", nav="tags",
        )

    for blog_id, plist in by_blog.items():
        src = sources.get(blog_id)
        _write_paged(
            env, f"blog/{blog_id}", "blog.html", plist, sources,
            title=src.name if src else blog_id, heading=src.name if src else blog_id,
            kind="blog", nav="blogs",
            extra={"source": src},
        )

    for y, plist in by_year.items():
        _write_paged(
            env, f"year/{y}", "list.html", plist, sources,
            title=f"Year {y}", heading=y, kind="year", nav="years",
        )

    write(
        "categories/index.html", "taxonomy.html",
        title="Categories", heading="Categories",
        items=sorted(((c, len(v)) for c, v in by_cat.items()), key=lambda x: -x[1]),
        prefix="category", nav="categories",
    )
    write(
        "tags/index.html", "taxonomy.html",
        title="Tags", heading="Tags",
        items=sorted(((t, len(v)) for t, v in by_tag.items()), key=lambda x: -x[1])[:500],
        prefix="tag", nav="tags",
    )
    write(
        "blogs/index.html", "blogs.html",
        title="Blogs",
        sources=sorted(sources.values(), key=lambda s: s.name.lower()),
        counts={b: len(v) for b, v in by_blog.items()},
        nav="blogs",
    )
    write(
        "years/index.html", "taxonomy.html",
        title="Years", heading="Years",
        items=sorted(((y, len(v)) for y, v in by_year.items()), key=lambda x: x[0], reverse=True),
        prefix="year", nav="years",
    )
    write(
        "sources/index.html", "sources.html",
        title="Sources & status",
        sources=sorted(sources.values(), key=lambda s: (s.status, s.name.lower())),
        counts={b: len(v) for b, v in by_blog.items()},
        last_run=store.load_json("last_run.json"),
        last_backfill=store.load_json("last_backfill.json"),
        nav="sources",
    )
    write("about/index.html", "about.html", title="About", nav="about")
    write("search/index.html", "search.html", title="Search", nav="search")

    (DIST / "feed.xml").write_text(_atom(posts[:100]), encoding="utf-8")

    export = [
        {
            "id": p.get("id"),
            "url": p["url"],
            "title": p.get("title"),
            "blog": p.get("blog"),
            "author": p.get("author"),
            "published": p.get("published"),
            "excerpt": p.get("excerpt"),
            "tags": p.get("tags") or [],
            "category": p.get("category"),
            "category_source": p.get("category_source"),
            "archive_url": p.get("archive_url"),
        }
        for p in posts
    ]
    (DIST / "export.json").write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
    (DIST / "export.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in export), encoding="utf-8"
    )
    (DIST / ".nojekyll").write_text("", encoding="utf-8")

    stats = {
        "posts": len(posts),
        "categories": {c: len(v) for c, v in sorted(by_cat.items(), key=lambda x: -len(x[1]))},
        "blogs": len(by_blog),
        "tags": len(by_tag),
        "tag_pages": len(top_tags),
        "years": sorted(by_year.keys()),
    }
    (DIST / "stats.json").write_text(json.dumps(stats, indent=2), encoding="utf-8")

    # One lightweight HTML page per post for Pagefind result URLs.
    # Browse pages stay paginated and are not indexed.
    entry_dir = DIST / "e"
    entry_dir.mkdir(parents=True, exist_ok=True)
    card_t = env.get_template("entry.html")
    for p in posts:
        eid = p.get("id") or slugify(p["url"])[:16]
        (entry_dir / f"{eid}.html").write_text(
            card_t.render(p=p, sources=sources, title=p.get("title") or "Post"),
            encoding="utf-8",
        )

    if pagefind:
        _run_pagefind()
    return stats


def _atom(posts: list[dict]) -> str:
    updated = (
        posts[0]["published"]
        if posts and posts[0].get("published")
        else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    entries = []
    for p in posts:
        entries.append(
            f"""  <entry>
    <title>{xml_escape(p.get('title') or '')}</title>
    <link href="{xml_escape(p['url'])}" rel="alternate"/>
    <link href="{xml_escape(p.get('archive_url') or '')}" rel="related" title="Wayback Machine"/>
    <id>tag:secblog,{p.get('id') or p['url']}</id>
    <updated>{xml_escape(p.get('published') or updated)}</updated>
    <summary>{xml_escape(p.get('excerpt') or '')}</summary>
    <category term="{xml_escape(p.get('category') or '')}"/>
  </entry>"""
        )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>secblog — security research archive</title>
  <link href="https://buffalotraceroute.github.io{BASE_PATH}/" rel="alternate"/>
  <link href="https://buffalotraceroute.github.io{BASE_PATH}/feed.xml" rel="self"/>
  <id>tag:buffalotraceroute.github.io,secblog</id>
  <updated>{updated}</updated>
  <subtitle>Metadata + short excerpts from security research blogs. Full text is never republished.</subtitle>
{chr(10).join(entries)}
</feed>
"""


def _run_pagefind() -> None:
    cmd = [
        sys.executable, "-m", "pagefind",
        "--site", str(DIST),
        "--output-subdir", "pagefind",
        "--glob", "e/**/*.html",
        "--force-language", "en",
    ]
    subprocess.run(cmd, check=False, cwd=str(ROOT))
