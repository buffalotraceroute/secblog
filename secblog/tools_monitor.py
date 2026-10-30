"""GitHub red-team tool monitor — metadata only (link-only policy).

Collects repo metadata from:
  - curated watchlist accounts (config/github_watch.yaml)
  - curated awesome-lists (generic mechanism)
  - GitHub code search discovery (rate-limited)

Never downloads tool source or binaries.
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from . import store, tools_store
from .sources import ROOT

CONFIG_PATH = ROOT / "config" / "github_watch.yaml"
API = "https://api.github.com"

CATEGORY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("BOFs", re.compile(r"\b(bof|beacon.?object.?file|coff.?loader)\b", re.I)),
    ("Loaders & Injection", re.compile(
        r"\b(shellcode.?loader|process.?injection|reflective.?dll|udrl|user.?defined.?reflective|"
        r"module.?stomping|dll.?injection|hollowing|coff.?loader|rdll)\b", re.I)),
    ("Evasion (EDR/AV/AMSI/ETW)", re.compile(
        r"\b(edr.?evas|edr.?bypass|av.?evas|amsi|etw|sleep.?obfus|sleep.?mask|syscalls?|"
        r"direct.?syscall|indirect.?syscall|unhook)\b", re.I)),
    ("Lateral Movement", re.compile(r"\b(lateral.?movement|wmi.?exec|smb.?exec|dcom|winrm|psexec)\b", re.I)),
    ("Credential Access", re.compile(
        r"\b(credential.?dump|lsass|mimikatz|nanodump|dpapi|sam.?dump|ntds|secretsdump|"
        r"kerberoast|asreproast|cred.?dump)\b", re.I)),
    ("Active Directory", re.compile(
        r"\b(active.?directory|bloodhound|ldap|kerberos|adcs|certipy|rubeus|impacket|"
        r"domain.?admin|dacl|acl.?abuse)\b", re.I)),
    ("C2 Frameworks & Agents", re.compile(
        r"\b(c2|command.?and.?control|cobalt.?strike|sliver|mythic|havoc|beacon|aggressor|"
        r"malleable|implant|teamserver)\b", re.I)),
    ("Persistence", re.compile(r"\b(persistence|scheduled.?task|startup|autostart|wmi.?event|com.?hijack)\b", re.I)),
    ("Privilege Escalation", re.compile(r"\b(privesc|privilege.?escalat|uac.?bypass|lpe|token.?impersonat)\b", re.I)),
    ("Recon", re.compile(r"\b(recon|situational.?awareness|enum|enumeration|port.?scan|fingerprint)\b", re.I)),
    ("Cloud", re.compile(r"\b(aws|azure|gcp|entra|kubernetes|k8s|cloud.?security|iam.?abuse)\b", re.I)),
]

CS_KIT_TAG_MAP = {
    "BOF": ["BOFs"],
    "Aggressor": ["C2 Frameworks & Agents", "Aggressor"],
    "UDRL": ["Loaders & Injection", "UDRL"],
    "RDLL": ["Loaders & Injection", "RDLL"],
    "Malleable-C2": ["C2 Frameworks & Agents", "Malleable-C2"],
    "UDC2": ["C2 Frameworks & Agents", "UDC2"],
    "External-C2": ["C2 Frameworks & Agents", "External-C2"],
    "REST API": ["C2 Frameworks & Agents"],
    "Kit": ["C2 Frameworks & Agents"],
}


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GITHUB_SECBLOG_TOKEN") or os.environ.get("GH_TOKEN")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def categorize(text: str, topics: list[str] | None = None, extra: list[str] | None = None) -> list[str]:
    blob = " ".join([text or "", " ".join(topics or []), " ".join(extra or [])])
    cats = [name for name, rx in CATEGORY_RULES if rx.search(blob)]
    return cats or ["Other"]


def is_lure(cfg: dict, name: str, description: str | None) -> bool:
    blob = f"{name} {description or ''}"
    for pat in cfg.get("lure_patterns") or []:
        if re.search(pat, blob, re.I):
            return True
    return False


class GitHubClient:
    def __init__(self, token: str | None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "secblog-tools-monitor/1.0",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=headers, follow_redirects=True)
        self.search_remaining = 30
        self.search_reset = 0.0
        self.core_remaining = 5000
        self.errors: list[str] = []

    async def close(self):
        await self.client.aclose()

    def _update_limits(self, r: httpx.Response, search: bool = False):
        try:
            rem = int(r.headers.get("X-RateLimit-Remaining", "0"))
            reset = float(r.headers.get("X-RateLimit-Reset", "0"))
            if search:
                self.search_remaining = rem
                self.search_reset = reset
            else:
                self.core_remaining = rem
        except Exception:
            pass

    async def _wait_search(self):
        if self.search_remaining <= 1:
            delay = max(0.0, self.search_reset - time.time()) + 1.0
            if delay > 0:
                await asyncio.sleep(min(delay, 65.0))

    async def get(self, path: str, params: dict | None = None, *, search: bool = False) -> Any | None:
        if search:
            await self._wait_search()
        url = path if path.startswith("http") else f"{API}{path}"
        try:
            r = await self.client.get(url, params=params)
            self._update_limits(r, search=search)
            if r.status_code == 404:
                return None
            if r.status_code == 403 and "rate limit" in r.text.lower():
                self.errors.append(f"rate-limit {path}")
                if search:
                    await asyncio.sleep(61)
                    return await self.get(path, params, search=search)
                return None
            if r.status_code >= 400:
                self.errors.append(f"{r.status_code} {path}")
                return None
            if r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return r.text
        except Exception as e:
            self.errors.append(f"{path}: {e}")
            return None

    async def get_raw(self, owner: str, repo: str, path: str) -> str | None:
        data = await self.get(f"/repos/{owner}/{repo}/contents/{path}", params={})
        # Prefer raw accept via dedicated call
        url = f"{API}/repos/{owner}/{repo}/contents/{path}"
        try:
            r = await self.client.get(url, headers={"Accept": "application/vnd.github.raw"})
            self._update_limits(r)
            if r.status_code == 200:
                return r.text
        except Exception as e:
            self.errors.append(f"raw {owner}/{repo}/{path}: {e}")
        return None

    async def user_repos(self, login: str, kind: str) -> list[dict]:
        base = f"/orgs/{login}/repos" if kind == "org" else f"/users/{login}/repos"
        out: list[dict] = []
        page = 1
        while page <= 10:
            params = {"sort": "created", "direction": "desc", "per_page": 100, "page": page}
            if kind != "org":
                params["type"] = "owner"
            else:
                params["type"] = "sources"
            batch = await self.get(base, params=params)
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    async def repo(self, full_name: str) -> dict | None:
        data = await self.get(f"/repos/{full_name}")
        return data if isinstance(data, dict) else None

    async def latest_release(self, full_name: str) -> dict | None:
        data = await self.get(f"/repos/{full_name}/releases/latest")
        if isinstance(data, dict) and data.get("tag_name"):
            return {
                "tag": data["tag_name"],
                "published_at": data.get("published_at"),
                "url": data.get("html_url"),
            }
        return None

    async def search_repos(self, query: str, per_page: int = 50) -> list[dict]:
        data = await self.get(
            "/search/repositories",
            params={"q": query, "sort": "stars", "order": "desc", "per_page": per_page},
            search=True,
        )
        if isinstance(data, dict):
            return data.get("items") or []
        return []


_GH_URL = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.I,
)


def parse_github_repos_from_markdown(text: str, skip_sections: list[str] | None = None) -> list[str]:
    """Extract owner/repo from markdown; optionally stop at skip section headings."""
    skip = [s.lower() for s in (skip_sections or [])]
    lines = text.splitlines()
    active = True
    found: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line.startswith("## "):
            title = line[3:].strip().lower()
            # strip emoji / leading symbols for match
            plain = re.sub(r"[^\w\s&/-]+", "", title).strip()
            if any(s.lower() in plain or s.lower() in title for s in skip):
                active = False
            elif title.startswith("#"):
                active = True
            else:
                # entering a new ## section: re-enable unless it's a skip
                active = not any(s.lower() in plain or s.lower() in title for s in skip)
        if not active:
            continue
        for m in _GH_URL.finditer(line):
            owner, repo = m.group(1), m.group(2)
            if owner.lower() in ("topics", "settings", "orgs", "marketplace", "features", "pricing", "about"):
                continue
            repo = repo.rstrip(").,;")
            if repo.endswith(".git"):
                repo = repo[:-4]
            # drop deep paths like blob/main/...
            if "/" in repo:
                continue
            full = f"{owner}/{repo}"
            key = full.lower()
            if key not in seen:
                seen.add(key)
                found.append(full)
    return found


def parse_community_kit_tracked(text: str) -> list[tuple[str, str]]:
    """Parse 'Category, owner/repo' lines → (full_name, kit_category)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," not in line:
            continue
        cat, rest = line.split(",", 1)
        cat, rest = cat.strip(), rest.strip()
        m = re.match(r"^([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", rest)
        if not m:
            continue
        full = f"{m.group(1)}/{m.group(2)}"
        if full.lower() in seen:
            continue
        seen.add(full.lower())
        out.append((full, cat))
    return out


def repo_to_tool(
    repo: dict,
    *,
    source: str,
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    affiliation: str | None = None,
    latest_release: dict | None = None,
) -> dict:
    full = repo.get("full_name") or f"{repo['owner']['login']}/{repo['name']}"
    owner = repo["owner"]["login"] if isinstance(repo.get("owner"), dict) else full.split("/")[0]
    desc = repo.get("description") or ""
    topics = list(repo.get("topics") or [])
    cats = list(categories or []) or categorize(f"{repo.get('name','')} {desc}", topics)
    return {
        "full_name": full,
        "name": repo.get("name") or full.split("/")[-1],
        "owner": owner,
        "url": repo.get("html_url") or f"https://github.com/{full}",
        "description": desc[:500] if desc else "",
        "language": repo.get("language") or "",
        "topics": topics,
        "stars": int(repo.get("stargazers_count") or 0),
        "forks": int(repo.get("forks_count") or 0),
        "created_at": repo.get("created_at") or "",
        "pushed_at": repo.get("pushed_at") or "",
        "archived": bool(repo.get("archived")),
        "categories": cats,
        "tags": list(tags or []),
        "source": source,
        "sources": [source],
        "affiliation": affiliation,
        "latest_release": latest_release,
        "fetched_at": _now(),
    }



async def _gather_repos(gh: GitHubClient, full_names: list[str], concurrency: int = 8) -> dict[str, dict]:
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, dict] = {}

    async def one(full: str):
        async with sem:
            repo = await gh.repo(full)
            if repo:
                out[full.lower()] = repo

    await asyncio.gather(*[one(f) for f in full_names])
    return out


async def collect_watchlist(gh: GitHubClient, cfg: dict, *, seed: bool) -> tuple[int, int, set[str]]:
    """Returns (new_or_updated, skipped, owner_logins_seen)."""
    wl = cfg.get("watchlist") or {}
    accounts = list(wl.get("accounts") or [])
    days = int(wl.get("created_since_days") if seed else wl.get("incremental_created_since_days") or 30)
    cutoff = _days_ago(days)
    changed = 0
    skipped = 0
    owners_seen: set[str] = set()
    # group saves by owner
    buckets: dict[str, dict[str, dict]] = {}

    for acct in accounts:
        login = acct["login"]
        kind = acct.get("type") or "user"
        track_all = bool(acct.get("track_all"))
        track_releases = bool(acct.get("track_releases"))
        affiliation = acct.get("affiliation")
        owners_seen.add(login.lower())
        repos = await gh.user_repos(login, kind)
        if login.lower() not in buckets:
            buckets[login.lower()] = tools_store.load_owner(login)
        bucket = buckets[login.lower()]
        for repo in repos:
            if repo.get("fork"):
                skipped += 1
                continue
            created = (repo.get("created_at") or "")[:10]
            if not track_all and created and created < cutoff:
                continue
            if is_lure(cfg, repo.get("name") or "", repo.get("description")):
                skipped += 1
                continue
            # Non-affiliated watchlist: skip empty description + Other + low stars (noise)
            if not track_all:
                desc = repo.get("description") or ""
                topics = list(repo.get("topics") or [])
                cats = categorize(f"{repo.get('name','')} {desc}", topics)
                stars = int(repo.get("stargazers_count") or 0)
                if cats == ["Other"] and stars < 5 and len(desc.strip()) < 8:
                    skipped += 1
                    continue
            rel = None
            if track_releases and (int(repo.get("stargazers_count") or 0) >= 3 or (created and created >= _days_ago(120))):
                rel = await gh.latest_release(repo["full_name"])
            tool = repo_to_tool(repo, source="watchlist", affiliation=affiliation, latest_release=rel)
            if tools_store.merge_tool(bucket, tool):
                changed += 1

    for owner, bucket in buckets.items():
        # find canonical casing from first tool
        if bucket:
            tools_store.save_owner(next(iter(bucket.values()))["owner"], bucket)
    return changed, skipped, owners_seen


async def collect_curated(gh: GitHubClient, cfg: dict, owners_seen: set[str]) -> tuple[int, dict[str, int], list[str]]:
    """Parse curated lists; return (changed, counts_by_list, new_owner_candidates)."""
    changed = 0
    counts: dict[str, int] = {}
    new_owners: list[str] = []
    buckets: dict[str, dict[str, dict]] = {}

    for clist in cfg.get("curated_lists") or []:
        cid = clist["id"]
        repo_full = clist["repo"]
        owner, name = repo_full.split("/", 1)
        source = clist.get("source_label") or f"curated:{cid}"
        default_cats = list(clist.get("default_categories") or [])
        cat_map = clist.get("category_map") or {}
        fmt = clist.get("format") or "markdown"
        entries: list[tuple[str, list[str], list[str]]] = []  # full_name, cats, tags

        kit_cats: dict[str, str] = {}
        kit_repos_payload: list[dict] = []
        for path in clist.get("paths") or []:
            text = await gh.get_raw(owner, name, path)
            if not text:
                continue
            if fmt in ("community_kit_tracked", "community_kit") and path.endswith(".txt"):
                for full, kit_cat in parse_community_kit_tracked(text):
                    kit_cats[full.lower()] = kit_cat
                    mapped = list(cat_map.get(kit_cat) or CS_KIT_TAG_MAP.get(kit_cat) or default_cats or ["Other"])
                    tags = [kit_cat] if kit_cat else []
                    entries.append((full, mapped, tags))
            elif fmt == "community_kit" and path.endswith(".json"):
                import json as _json
                try:
                    payload = _json.loads(text)
                    rows = payload.get("data") if isinstance(payload, dict) else payload
                    if isinstance(rows, list):
                        kit_repos_payload = [r for r in rows if isinstance(r, dict) and r.get("full_name")]
                except Exception as e:
                    gh.errors.append(f"community_kit json: {e}")
            elif fmt not in ("community_kit", "community_kit_tracked"):
                for full in parse_github_repos_from_markdown(text, skip_sections=clist.get("skip_sections")):
                    if full.lower() == repo_full.lower():
                        continue
                    entries.append((full, list(default_cats) or ["BOFs"], []))

        # dedupe entries preserving first cats
        seen: set[str] = set()
        uniq: list[tuple[str, list[str], list[str]]] = []
        for full, cats, tags in entries:
            k = full.lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append((full, cats, tags))
        counts[cid] = len(uniq)

        by_payload = {r["full_name"].lower(): r for r in kit_repos_payload}
        missing = [full for full, _, _ in uniq if full.lower() not in by_payload]
        if missing:
            fetched = await _gather_repos(gh, missing, concurrency=10)
            by_payload.update(fetched)
        for full, cats, tags in uniq:
            o = full.split("/")[0]
            if clist.get("add_owners_to_watch") and o.lower() not in owners_seen:
                new_owners.append(o)
                owners_seen.add(o.lower())
            if o.lower() not in buckets:
                buckets[o.lower()] = tools_store.load_owner(o)
            repo = by_payload.get(full.lower())
            if not repo or repo.get("fork"):
                continue
            if is_lure(cfg, repo.get("name") or "", repo.get("description")):
                continue
            kw = categorize(f"{repo.get('name','')} {repo.get('description') or ''}", list(repo.get("topics") or []))
            merged = set(cats) | set(kw)
            if merged - {"Other"}:
                merged.discard("Other")
            all_cats = sorted(merged) or (cats or ["Other"])
            tool = repo_to_tool(repo, source=source, categories=all_cats, tags=tags)
            if tools_store.merge_tool(buckets[o.lower()], tool):
                changed += 1

    for bucket in buckets.values():
        if bucket:
            tools_store.save_owner(next(iter(bucket.values()))["owner"], bucket)
    # unique new owners preserving order
    seen_o: set[str] = set()
    ordered: list[str] = []
    for o in new_owners:
        if o.lower() not in seen_o:
            seen_o.add(o.lower())
            ordered.append(o)
    return changed, counts, ordered


async def collect_discovery(gh: GitHubClient, cfg: dict, *, seed: bool) -> int:
    disc = cfg.get("discovery") or {}
    queries = list(disc.get("queries") or [])
    max_q = int(disc.get("max_queries_per_run") or 12)
    days = int(disc.get("seed_lookback_days") if seed else disc.get("lookback_days") or 14)
    min_stars = int(disc.get("min_stars") or 5)
    cutoff = _days_ago(days)
    # rotate queries using day-of-year so incremental runs cover the set
    if not seed and len(queries) > max_q:
        start = datetime.now(timezone.utc).timetuple().tm_yday % len(queries)
        queries = (queries[start:] + queries[:start])[:max_q]
    else:
        queries = queries[:max_q]

    changed = 0
    buckets: dict[str, dict[str, dict]] = {}
    for q in queries:
        full_q = f"{q} created:>={cutoff} fork:false"
        items = await gh.search_repos(full_q, per_page=40)
        for repo in items:
            if repo.get("fork") or repo.get("archived"):
                continue
            if int(repo.get("stargazers_count") or 0) < min_stars:
                continue
            desc = repo.get("description") or ""
            if not desc.strip() and int(repo.get("stargazers_count") or 0) < 20:
                continue
            if is_lure(cfg, repo.get("name") or "", desc):
                continue
            # very-new account + exe-only heuristic (metadata): skip empty-desc + name looks like crack
            owner = repo["owner"]["login"] if isinstance(repo.get("owner"), dict) else repo["full_name"].split("/")[0]
            if owner.lower() not in buckets:
                buckets[owner.lower()] = tools_store.load_owner(owner)
            tool = repo_to_tool(repo, source="discovery")
            if tools_store.merge_tool(buckets[owner.lower()], tool):
                changed += 1

    for bucket in buckets.values():
        if bucket:
            tools_store.save_owner(next(iter(bucket.values()))["owner"], bucket)
    return changed


async def verify_and_extend_watchlist(gh: GitHubClient, cfg: dict, candidates: list[str]) -> list[dict]:
    """Verify candidate logins exist; return new account entries (not yet in config file)."""
    existing = {a["login"].lower() for a in (cfg.get("watchlist") or {}).get("accounts") or []}
    added: list[dict] = []
    for login in candidates:
        if login.lower() in existing:
            continue
        data = await gh.get(f"/users/{login}")
        if not isinstance(data, dict) or not data.get("login"):
            continue
        entry = {
            "login": data["login"],
            "type": "org" if data.get("type") == "Organization" else "user",
            "via_curated": True,
        }
        added.append(entry)
        existing.add(login.lower())
    return added


def persist_dynamic_watch_owners(new_accounts: list[dict]) -> None:
    """Append curated-derived owners into data/state/tools_watch_extra.json (runtime watch)."""
    path_name = "tools_watch_extra.json"
    cur = store.load_json(path_name)
    accounts = list(cur.get("accounts") or [])
    have = {a["login"].lower() for a in accounts}
    for a in new_accounts:
        if a["login"].lower() not in have:
            accounts.append(a)
            have.add(a["login"].lower())
    store.save_json(path_name, {"accounts": accounts, "updated": _now()})


def merge_extra_watch(cfg: dict) -> dict:
    extra = store.load_json("tools_watch_extra.json")
    accounts = list((cfg.get("watchlist") or {}).get("accounts") or [])
    have = {a["login"].lower() for a in accounts}
    for a in extra.get("accounts") or []:
        if a.get("login") and a["login"].lower() not in have:
            accounts.append(a)
            have.add(a["login"].lower())
    cfg = dict(cfg)
    wl = dict(cfg.get("watchlist") or {})
    wl["accounts"] = accounts
    cfg["watchlist"] = wl
    return cfg


async def run(*, seed: bool = False, skip_discovery: bool = False, skip_curated: bool = False) -> dict:
    """Run a tools collection cycle. Never raises for API errors — returns summary."""
    cfg = _load_config()
    cfg = merge_extra_watch(cfg)
    token = _token()
    gh = GitHubClient(token)
    summary: dict[str, Any] = {
        "started": _now(),
        "seed": seed,
        "authenticated": bool(token),
        "watchlist_changed": 0,
        "curated_changed": 0,
        "discovery_changed": 0,
        "curated_counts": {},
        "new_watch_owners": 0,
        "errors": [],
        "watched_accounts": len((cfg.get("watchlist") or {}).get("accounts") or []),
    }
    try:
        owners_seen = {a["login"].lower() for a in (cfg.get("watchlist") or {}).get("accounts") or []}
        try:
            w_changed, _, _ = await collect_watchlist(gh, cfg, seed=seed)
            summary["watchlist_changed"] = w_changed
        except Exception as e:
            summary["errors"].append(f"watchlist: {e}")

        if not skip_curated:
            try:
                c_changed, counts, new_owners = await collect_curated(gh, cfg, owners_seen)
                summary["curated_changed"] = c_changed
                summary["curated_counts"] = counts
                verified = await verify_and_extend_watchlist(gh, cfg, new_owners)
                if verified:
                    persist_dynamic_watch_owners(verified)
                    summary["new_watch_owners"] = len(verified)
                    # Fetch only newly verified curated owners (not the whole watchlist again)
                    cfg2 = dict(cfg)
                    cfg2["watchlist"] = {
                        **(cfg.get("watchlist") or {}),
                        # Curated owners: watch for *new* repos only (linked repos already ingested)
                        "accounts": verified,
                    }
                    w2, _, _ = await collect_watchlist(gh, cfg2, seed=seed)
                    summary["watchlist_changed"] += w2
                    summary["watched_accounts"] = len(
                        (merge_extra_watch(cfg).get("watchlist") or {}).get("accounts") or []
                    )
            except Exception as e:
                summary["errors"].append(f"curated: {e}")

        if not skip_discovery:
            try:
                summary["discovery_changed"] = await collect_discovery(gh, cfg, seed=seed)
            except Exception as e:
                summary["errors"].append(f"discovery: {e}")

        summary["errors"].extend(gh.errors[:50])
        tools = tools_store.load_all_tools()
        summary["tools_total"] = len(tools)
        by_cat: dict[str, int] = {}
        for t in tools:
            for c in t.get("categories") or ["Other"]:
                by_cat[c] = by_cat.get(c, 0) + 1
        summary["by_category"] = dict(sorted(by_cat.items(), key=lambda x: -x[1]))
        summary["finished"] = _now()
        store.save_json(tools_store.STATE_NAME, summary)
        return summary
    finally:
        await gh.close()
