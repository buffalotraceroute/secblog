"""Git-friendly tool metadata storage under data/tools/<owner>.jsonl.

Policy: link-only metadata. Never download, host, or mirror tool code/binaries.
"""
from __future__ import annotations

import json
from pathlib import Path

from .sources import ROOT

TOOLS_DIR = ROOT / "data" / "tools"
STATE_NAME = "tools_last_run.json"

FIELDS = [
    "full_name", "name", "owner", "url", "description", "language", "topics",
    "stars", "forks", "created_at", "pushed_at", "archived", "categories",
    "tags", "source", "sources", "affiliation", "latest_release", "fetched_at",
]


def _path(owner: str) -> Path:
    safe = owner.replace("/", "_")
    return TOOLS_DIR / f"{safe}.jsonl"


def load_owner(owner: str) -> dict[str, dict]:
    path = _path(owner)
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = json.loads(line)
                out[t["full_name"].lower()] = t
    return out


def save_owner(owner: str, tools: dict[str, dict]) -> None:
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    path = _path(owner)
    rows = sorted(tools.values(), key=lambda t: (t.get("created_at") or "", t["full_name"]), reverse=True)
    path.write_text(
        "".join(json.dumps(_normalize(t), ensure_ascii=False, sort_keys=True) + "\n" for t in rows),
        encoding="utf-8",
    )


def _normalize(t: dict) -> dict:
    out = {k: t.get(k) for k in FIELDS if k in t or k in ("topics", "categories", "tags", "sources")}
    out["full_name"] = t["full_name"]
    out["name"] = t.get("name") or out["full_name"].split("/")[-1]
    out["owner"] = t.get("owner") or out["full_name"].split("/")[0]
    out["url"] = t.get("url") or f"https://github.com/{out['full_name']}"
    out["topics"] = sorted(set(out.get("topics") or []))
    out["categories"] = sorted(set(out.get("categories") or []))
    out["tags"] = sorted(set(out.get("tags") or []))
    srcs = set(out.get("sources") or [])
    if t.get("source"):
        srcs.add(t["source"])
    out["sources"] = sorted(srcs)
    out.pop("source", None)
    # drop empties except required
    clean = {}
    for k, v in out.items():
        if v in (None, "", []):
            if k in ("full_name", "name", "owner", "url", "topics", "categories", "tags", "sources"):
                clean[k] = v if v not in (None,) else ([] if k.endswith("s") else "")
            continue
        clean[k] = v
    for k in ("topics", "categories", "tags", "sources"):
        clean.setdefault(k, [])
    return clean


def merge_tool(bucket: dict[str, dict], new: dict) -> bool:
    """Insert/update by full_name. Returns True if changed."""
    key = new["full_name"].lower()
    old = bucket.get(key)
    if old is None:
        bucket[key] = _normalize(new)
        return True
    changed = False
    for k in ("stars", "forks", "pushed_at", "description", "language", "archived", "latest_release", "fetched_at"):
        v = new.get(k)
        if v is not None and old.get(k) != v:
            old[k] = v
            changed = True
    for k in ("topics", "categories", "tags"):
        before = set(old.get(k) or [])
        after = before | set(new.get(k) or [])
        if after != before:
            old[k] = sorted(after)
            changed = True
    srcs = set(old.get("sources") or [])
    add = set(new.get("sources") or [])
    if new.get("source"):
        add.add(new["source"])
    if add - srcs:
        old["sources"] = sorted(srcs | add)
        changed = True
    if new.get("affiliation") and not old.get("affiliation"):
        old["affiliation"] = new["affiliation"]
        changed = True
    if changed:
        bucket[key] = _normalize(old)
    return changed


def iter_all_tools():
    if not TOOLS_DIR.exists():
        return
    for path in sorted(TOOLS_DIR.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def load_all_tools() -> list[dict]:
    tools = list(iter_all_tools() or [])
    # dedupe if any
    by: dict[str, dict] = {}
    for t in tools:
        by[t["full_name"].lower()] = t
    return sorted(by.values(), key=lambda t: (t.get("created_at") or "", t["full_name"]), reverse=True)
