#!/usr/bin/env python3
"""One-off repair: rebase post URLs that point at a dev server (0.0.0.0:4000,
localhost, 127.x) or have no host ("https:///posts/x/") onto the source's real
homepage host, fix archive_url / id accordingly, and merge duplicates.

Usage: python tools/fix_bad_hosts.py [--dry-run] [--list FILE]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from secblog import store  # noqa: E402
from secblog.filters import fix_post_urls  # noqa: E402
from secblog.util import normalize_url, post_id  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", help="write repaired URLs as JSONL (blog, old, new) here")
    args = ap.parse_args()
    out = open(args.list, "w") if args.list else None
    total = dup_total = 0
    for path in sorted(store.POSTS_DIR.glob("*.jsonl")):
        blog = path.stem
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        good, fixed = [], []
        for p in rows:
            old = p["url"]
            if fix_post_urls(p, blog):
                p["id"] = post_id(p["url"])
                fixed.append(p)
                if out:
                    out.write(json.dumps({"blog": blog, "old": old, "new": p["url"]}) + "\n")
            else:
                good.append(p)
        if not fixed:
            continue
        posts = {normalize_url(p["url"]): p for p in good}
        dups = 0
        for p in fixed:
            if normalize_url(p["url"]) in posts:
                dups += 1
            store.merge_post(posts, p)  # keeps the better record on collision
        total += len(fixed)
        dup_total += dups
        print(f"{blog:32} repaired {len(fixed):4}  merged-duplicates {dups:3}  entries {len(rows)} -> {len(posts)}")
        if not args.dry_run:
            store.save_blog(blog, posts)
    print(f"total repaired {total}, duplicates merged {dup_total}{' (dry run)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
