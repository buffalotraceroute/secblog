#!/usr/bin/env python3
"""Git merge driver for generated data (data/posts, data/tools, data/state).

Two "Collect and deploy" runs that overlap (e.g. a push queued behind the
6-hourly schedule) both rewrite the same JSONL/JSON files; a line-based rebase
then conflicts and the run fails. This driver merges records instead of lines:

  data/posts/*.jsonl  union keyed by normalized URL; on collision the incoming
                      record is merged in with store.merge_post semantics
  data/tools/*.jsonl  union keyed by full_name; incoming record wins
  data/**/*.json      shallow dict merge; incoming keys win

Configured by tools/ci_commit_data.sh:
  git config merge.secblog-data.driver "python tools/merge_data.py %O %A %B %P"
with `data/** merge=secblog-data` in .gitattributes.
%A (current/upstream) is overwritten with the result; %B is the commit being
replayed (this run's fresh data). Exit 0 = clean merge, 1 = leave conflicted.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _jsonl(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def merge_posts(a: str, b: str, name: str) -> str:
    from secblog import store
    from secblog.filters import post_reject_reason
    from secblog.util import normalize_url

    blog = Path(name).stem
    posts = {normalize_url(p["url"]): p for p in _jsonl(a)}
    for p in _jsonl(b):
        store.merge_post(posts, p)
    rows = sorted((p for p in posts.values() if post_reject_reason(p, blog) is None),
                  key=store._sort_key, reverse=True)
    return "".join(json.dumps(store._clean(p), ensure_ascii=False, sort_keys=True) + "\n" for p in rows)


def merge_tools(a: str, b: str) -> str:
    from secblog import tools_store

    tools = {t["full_name"].lower(): t for t in _jsonl(a)}
    for t in _jsonl(b):
        tools[t["full_name"].lower()] = t
    rows = sorted(tools.values(), key=lambda t: (t.get("created_at") or "", t["full_name"]), reverse=True)
    return "".join(json.dumps(tools_store._normalize(t), ensure_ascii=False, sort_keys=True) + "\n" for t in rows)


def merge_json(a: str, b: str) -> str:
    da = json.loads(Path(a).read_text(encoding="utf-8") or "{}")
    db = json.loads(Path(b).read_text(encoding="utf-8") or "{}")
    if not (isinstance(da, dict) and isinstance(db, dict)):
        out = db
    else:
        out = {**da, **db}
    return json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    _o, a, b, name = argv[1:5]
    try:
        if name.startswith("data/posts/") and name.endswith(".jsonl"):
            text = merge_posts(a, b, name)
        elif name.startswith("data/tools/") and name.endswith(".jsonl"):
            text = merge_tools(a, b)
        elif name.endswith(".json"):
            text = merge_json(a, b)
        else:
            return 1
    except Exception as e:  # leave the file conflicted rather than corrupt it
        print(f"merge_data: {name}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    Path(a).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
