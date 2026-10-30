#!/usr/bin/env python3
"""One-off: build sources.yaml from blogs.txt + a liveness-check CSV.

Usage: python tools/bootstrap_sources.py blogs.txt results.csv > sources.yaml

Kept in the repo for reference/regeneration; after the first run sources.yaml
is the hand-edited source of truth. Dead blogs are never fetched live.
"""
from __future__ import annotations

import asyncio
import csv
import re
import sys
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
from secblog.util import BROWSER_USER_AGENT, clean_url, force_https, slugify  # noqa: E402

SPAM = re.compile(r"\b(togel|sbobet|slot gacor|casino|judi|gacor|poker online|bandar|viagra|escort)\b", re.I)
STATUS_MAP = {
    "Active": "active", "Slowing": "slowing", "Dormant": "dormant", "Alive-unknown": "unknown",
    "Blocked (likely alive)": "blocked", "Dead": "dead",
}
CANDIDATES = ["feed/", "feed", "rss.xml", "atom.xml", "index.xml", "feed.xml", "rss/", "rss", "blog/feed/",
              "blog/rss.xml", "blog/index.xml", "feed.atom", "?feed=rss2"]


def make_id(url: str, used: set[str]) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    path = p.path.strip("/")
    if host in ("github.com", "medium.com") or host.endswith(("github.io", "gitbook.io")) and path:
        base = f"{host.split('.')[0] if host.endswith(('github.io','gitbook.io')) else host.split('.')[0]}-{path.split('/')[0]}" if host.endswith(("github.io", "gitbook.io")) else f"{host.split('.')[0]}-{path.replace('/', '-')}"
        if host.endswith(("github.io", "gitbook.io")):
            base = f"{host.split('.')[0]}-{path}"
    else:
        base = host
        for suf in (".blogspot.com", ".github.io", ".wordpress.com", ".substack.com"):
            if base.endswith(suf):
                base = base[: -len(suf)]
        base = re.sub(r"\.(com|net|org|io|co\.uk|de|fr|me|sh|pw|wtf|club|tech|re|ch|no|nl|cy|tf|info|blog|wiki|dev|ai|us|ca|ph|link|codes|party|pro|studio|systems|uk|vision|consulting|security)$", "", base)
        if path and not re.fullmatch(r"(blog|blogs|posts|research|en/blog|tech-blog|publications)", path, re.I) and host not in ("github.com",):
            # keep path segments that distinguish several blogs on one host
            base = f"{base}-{path}"
    ident = slugify(base)[:60]
    i, cand = 2, ident
    while cand in used:
        cand, i = f"{ident}-{i}", i + 1
    used.add(cand)
    return cand


def feed_ok(content: bytes) -> tuple[bool, str]:
    d = feedparser.parse(content)
    if not d.entries:
        return False, ""
    titles = " ".join((e.get("title") or "") for e in d.entries[:20])
    if SPAM.search(titles):
        return False, ""
    return True, (d.feed.get("title") or "").strip()


async def fetch(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url)
        return r
    except Exception:
        return None


async def discover(client, sem, row):
    url = force_https(row["url"].strip())
    final = row.get("final_url") or url
    status = STATUS_MAP.get(row["category"], "unknown")
    out = {"url": url, "final": final, "status": status, "feeds": [], "title": "", "notes": row.get("notes", "")}
    if status == "dead":
        return out
    host = (urlsplit(url).hostname or "").lower()
    # GitHub repositories: commits + releases Atom feeds.
    m = re.match(r"https://github\.com/([^/]+)/([^/?#]+)", final) or re.match(r"https://github\.com/([^/]+)/([^/?#]+)", url)
    if m:
        o, r_ = m.group(1), m.group(2)
        out["feeds"] = [f"https://github.com/{o}/{r_}/commits.atom", f"https://github.com/{o}/{r_}/releases.atom"]
        out["title"] = f"{o}/{r_}"
        out["homepage"] = f"https://github.com/{o}/{r_}"
        return out
    # Medium users / publications.
    m = re.match(r"https://medium\.com/(@?[^/?#]+)", url)
    if m:
        cand = [f"https://medium.com/feed/{m.group(1)}"]
    else:
        cand = []
    if row.get("feed_url"):
        fu = row["feed_url"]
        fu = re.sub(r"(?<!:)//+", "/", fu)
        cand.insert(0, force_https(fu))
    async with sem:
        page = await fetch(client, final if final.startswith("https") else url)
        if page is not None and page.status_code == 200 and "html" in page.headers.get("content-type", ""):
            t = re.search(r"<title[^>]*>(.*?)</title>", page.text[:200000], re.S | re.I)
            if t:
                out["title"] = re.sub(r"\s+", " ", t.group(1)).strip()
            for lm in re.finditer(r"<link\b[^>]*>", page.text[:300000], re.I):
                tag = lm.group(0)
                if re.search(r"rel=[\"']?alternate", tag, re.I) and re.search(r"(rss|atom)\+xml", tag, re.I):
                    h = re.search(r"href=[\"']([^\"']+)", tag)
                    if h and "comments" not in h.group(1):
                        cand.append(force_https(urljoin(str(page.url), h.group(1))))
        base = str(page.url) if page is not None else final
        if not base.endswith("/"):
            base_dir = base + "/"
        else:
            base_dir = base
        root = f"https://{urlsplit(base).netloc}/"
        if host.endswith("blogspot.com"):
            cand.append(root + "feeds/posts/default")
        for c in CANDIDATES:
            cand.append(urljoin(base_dir, c))
            cand.append(urljoin(root, c))
        seen = set()
        for c in cand:
            if c in seen:
                continue
            seen.add(c)
            r = await fetch(client, c)
            if r is None or r.status_code != 200:
                continue
            ok, ftitle = feed_ok(r.content)
            if ok:
                out["feeds"].append(clean_url(str(r.url)) if "?feed=" not in c else c)
                if ftitle:
                    out["title"] = ftitle
                break
            if len(seen) > 14:
                break
    return out


def yq(s: str) -> str:
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


async def main():
    urls = []
    for line in open(sys.argv[1], encoding="utf-8"):
        u = line.strip()
        if u and u not in urls:
            urls.append(u)
    rows = {r["url"].strip(): r for r in csv.DictReader(open(sys.argv[2], encoding="utf-8"))}
    sem = asyncio.Semaphore(24)
    limits = httpx.Limits(max_connections=48)
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, limits=limits, verify=False,
                                 headers={"User-Agent": BROWSER_USER_AGENT}) as client:
        results = await asyncio.gather(*(discover(client, sem, rows.get(u) or {"url": u, "category": "Alive-unknown"}) for u in urls))
    used: set[str] = set()
    print("# secblog source registry. Hand-edit freely; see README 'Adding a blog'.")
    print("# Fields: id (stable slug, used for data/posts/<id>.jsonl), name, homepage, feeds (list),")
    print("#   status: active|slowing|dormant|unknown|blocked|dead   (dead = never fetched live; Wayback only)")
    print("#   optional: category (default primary category), platform (blogspot|wordpress|medium|github|generic),")
    print("#   sitemap, wayback_domain, wayback_before (YYYYMMDD; ignore captures after a domain was hijacked),")
    print("#   backfill: false, notes")
    print("sources:")
    for res in results:
        homepage = res.get("homepage") or res["url"]
        if res["status"] != "dead" and res["final"] and urlsplit(res["final"]).hostname == urlsplit(res["url"]).hostname:
            homepage = res["final"]
        sid = make_id(res["url"], used)
        name = res["title"] or (urlsplit(res["url"]).hostname or sid).removeprefix("www.")
        name = re.sub(r"\s+", " ", name)[:80]
        if SPAM.search(name):
            name = (urlsplit(res["url"]).hostname or sid).removeprefix("www.")
        print(f"  - id: {sid}")
        print(f"    name: {yq(name)}")
        print(f"    homepage: {clean_url(homepage)}")
        if res["feeds"]:
            print("    feeds:")
            for f in res["feeds"]:
                print(f"      - {f}")
        else:
            print("    feeds: []")
        print(f"    status: {res['status']}")
        if res["status"] == "dead":
            p = urlsplit(res["url"])
            print(f"    wayback_domain: {(p.hostname or '').removeprefix('www.')}")
        if "github.com" in homepage:
            print("    category: Tools & Releases")
        notes = res["notes"]
        if notes and (res["status"] in ("dead", "blocked") or "cross-domain" in notes or "hijack" in notes):
            print(f"    notes: {yq(notes[:160])}")


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    asyncio.run(main())
