"""Load and validate the sources.yaml registry."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.yaml"

# Status values used across the project (derived from the liveness check, editable by hand).
STATUSES = ["active", "slowing", "dormant", "unknown", "blocked", "dead"]
STATUS_LABELS = {
    "active": "Active",
    "slowing": "Slowing",
    "dormant": "Dormant",
    "unknown": "Alive (no feed)",
    "blocked": "Blocked (likely alive)",
    "dead": "Dead (archived)",
}


@dataclass
class Source:
    id: str
    name: str
    homepage: str
    feeds: list[str] = field(default_factory=list)
    status: str = "unknown"
    category: str | None = None
    platform: str | None = None
    sitemap: str | None = None
    wayback_before: str | None = None  # only use Wayback captures older than this (YYYYMMDD)
    wayback_domain: str | None = None  # domain/path prefix to query in the Wayback CDX
    backfill: bool = True
    notes: str | None = None

    @property
    def is_dead(self) -> bool:
        return self.status == "dead"

    @property
    def host(self) -> str:
        return (urlsplit(self.homepage).hostname or "").lower()

    def detected_platform(self) -> str:
        if self.platform:
            return self.platform
        h = self.host
        feeds = " ".join(self.feeds)
        if h == "github.com":
            return "github"
        if h.endswith("blogspot.com") or "/feeds/posts/default" in feeds:
            return "blogspot"
        if h.endswith("medium.com") or "medium.com/feed" in feeds:
            return "medium"
        if h.endswith("substack.com") or feeds.endswith("/feed") and "substack" in feeds:
            return "substack"
        if re.search(r"/feed/?$", feeds) or "wordpress" in h:
            return "wordpress"
        return "generic"


def load_sources(path: Path = SOURCES_FILE) -> list[Source]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    items = raw.get("sources", raw) if isinstance(raw, dict) else raw
    out: list[Source] = []
    seen: set[str] = set()
    for item in items:
        feeds = item.get("feeds") or []
        if isinstance(feeds, str):
            feeds = [feeds]
        src = Source(
            id=str(item["id"]),
            name=str(item.get("name") or item["id"]),
            homepage=item["homepage"],
            feeds=[f for f in feeds if f],
            status=str(item.get("status") or "unknown").lower(),
            category=item.get("category"),
            platform=item.get("platform"),
            sitemap=item.get("sitemap"),
            wayback_before=str(item["wayback_before"]) if item.get("wayback_before") else None,
            wayback_domain=item.get("wayback_domain"),
            backfill=item.get("backfill", True),
            notes=item.get("notes"),
        )
        if src.id in seen:
            raise ValueError(f"duplicate source id in sources.yaml: {src.id}")
        if src.status not in STATUSES:
            raise ValueError(f"{src.id}: unknown status {src.status!r} (expected one of {STATUSES})")
        seen.add(src.id)
        out.append(src)
    return out
