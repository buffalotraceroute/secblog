#!/usr/bin/env python3
"""One-off cleanup: drop non-article entries (tag/category/author/listing pages,
static pages, feeds, assets...) from data/posts/*.jsonl using the same shared
filter the collectors use (secblog/filters.py).

Usage: python tools/clean_non_articles.py [--dry-run] [--show N]
Kept lines are written back verbatim, so the diff only contains removals.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from secblog.filters import post_reject_reason  # noqa: E402
from secblog.store import POSTS_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--show", type=int, default=0, help="print up to N removed URLs per file")
    args = ap.parse_args()

    total_before = total_after = 0
    reasons: collections.Counter[str] = collections.Counter()
    rows = []
    for path in sorted(POSTS_DIR.glob("*.jsonl")):
        blog = path.stem
        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        kept, removed = [], []
        for line in lines:
            why = post_reject_reason(json.loads(line), blog)
            if why:
                removed.append((json.loads(line)["url"], why))
                reasons[why.split(":")[0] if why.startswith(("exclude_pattern", "query")) else why] += 1
            else:
                kept.append(line)
        total_before += len(lines)
        total_after += len(kept)
        if removed:
            rows.append((blog, len(lines), len(kept), removed))
            if not args.dry_run:
                if kept:
                    path.write_text("".join(l + "\n" for l in kept), encoding="utf-8")
                else:
                    path.unlink()

    rows.sort(key=lambda r: -(r[1] - r[2]))
    print(f"{'blog':45} {'before':>7} {'after':>7} {'removed':>8}")
    for blog, before, after, removed in rows:
        print(f"{blog:45} {before:7} {after:7} {before - after:8}")
        for url, why in removed[: args.show]:
            print(f"      - {why:28} {url}")
    print(f"\nfiles changed: {len(rows)}; entries before: {total_before}, after: {total_after}, "
          f"removed: {total_before - total_after}{' (dry run)' if args.dry_run else ''}")
    print("by reason:", dict(reasons.most_common()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
