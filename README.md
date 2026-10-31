# secblog

A public, searchable archive of **security research blog posts**, hosted on GitHub Pages at
[buffalotraceroute.github.io/secblog](https://buffalotraceroute.github.io/secblog/).

The site stores and shows **metadata + a short excerpt (~300 characters)** only, with links to
the original post and a [Wayback Machine](https://web.archive.org/) snapshot. Full post text is
never stored or republished.

Blog list seeded from [Bad Sector Labs' security blog list](https://github.com/BadSectorLabs/bad-sector-labs-blog-list) — thank you.

## How it works

1. **`sources.yaml`** — hand-editable registry of blogs (name, homepage, feed URLs, status).
2. **Collector** (`python -m secblog collect`) — polls RSS/Atom feeds with conditional requests
   (ETag / Last-Modified cached in `data/state/etag.json`). One bad feed never fails a run.
3. **Backfill** (`python -m secblog backfill`) — resumable history via Blogspot `start-index`,
   WordPress `?paged=` / REST API, sitemaps, and the Wayback CDX API for **dead** blogs.
4. **Tagging** — deterministic rules in `config/taxonomy.yaml` (categories, CVEs, ATT&CK, tools…).
   Optional LLM hook behind `SECBLOG_LLM_TAGGER=1` (off by default).
5. **Tools monitor** (`python -m secblog tools`) — watches curated GitHub orgs/users
   (`config/github_watch.yaml`), re-parses curated lists (awesome-bof, Cobalt Strike
   Community Kit, …), and runs modest discovery searches. Stores **repo metadata only**
   under `data/tools/` (link-only policy — never mirrors code/binaries).
6. **Site** — Jinja2 static generator + [Pagefind](https://pagefind.app/) client-side search
   (blog posts + Tools).
7. **GitHub Actions** — collect + tools every 6 hours + deploy Pages; weekly resumable backfill.

Dead / hijacked domains (parking, spam redirects) are **never fetched or linked live**; only
Wayback snapshots are ingested. Set `wayback_before` on a source to ignore captures after a
hijack date.

## Adding a blog

Set `default_category` on a source when unmatched posts should inherit a blog-level
topic (e.g. research labs). Genuine news/aggregators are listed in
`config/taxonomy.yaml` → `news_blogs` and stay **News & Industry**. Each post records
`category_source`: `keyword` or `blog_default`.


Edit `sources.yaml`:

```yaml
  - id: example-blog          # stable slug → data/posts/example-blog.jsonl
    name: "Example Blog"
    homepage: https://example.com/blog/
    feeds:
      - https://example.com/blog/feed/
    status: active            # active|slowing|dormant|unknown|blocked|dead
    category: "Web & AppSec"  # optional default primary category
    platform: wordpress       # optional: blogspot|wordpress|medium|github|generic
    via: auto                 # optional: auto (default) | direct | feedly
```

Tips:

- Medium: `https://medium.com/feed/<@user-or-publication>`
- Blogspot: `https://<name>.blogspot.com/feeds/posts/default`
- GitHub repo: `https://github.com/<org>/<repo>/commits.atom` (and/or `releases.atom`)
- Dead blog: `status: dead`, set `wayback_domain`, optional `wayback_before: "YYYYMMDD"`, leave `feeds` empty or pointing at a historical feed URL (fetched only via Wayback)
- Non-article pages: tag/category/author/series listings, blog indexes, pagination, about/contact/privacy-type pages, feeds and sitemap-found assets are never stored (shared filter in `secblog/filters.py`, applied to every ingestion path and on save). Add per-source regexes with `exclude_patterns: ["^/en/about/"]` (matched against the URL path). `python tools/clean_non_articles.py` re-applies the filter to existing `data/posts/`.
- Cloudflare/bot-walled feed: `via: feedly` reads the feed from Feedly's public cache (`cloud.feedly.com/v3/streams/contents`, no auth; backfill pages its history via continuation tokens). With the default `via: auto`, a direct fetch that gets a 403/Cloudflare challenge falls back to Feedly automatically; `via: direct` disables that. Excerpt-only policy still applies.

## Local development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Poll all live feeds (incremental)
python -m secblog collect

# GitHub red-team tools metadata (watchlist + curated lists + discovery)
python -m secblog tools
# Initial seed (wider lookback):
python -m secblog tools --seed

# Historical backfill (~35 min budget by default)
python -m secblog backfill --budget 2100

# Build static site into dist/ (includes Pagefind index)
python -m secblog build

# Serve locally with the /secblog/ base path
python -m secblog serve --port 8080
# → http://127.0.0.1:8080/secblog/
```

Or all three: `python -m secblog all --budget 2100`.

Data layout: one JSONL file per blog under `data/posts/<id>.jsonl` (git-friendly, scales to
tens of thousands of posts). Tool metadata: `data/tools/<owner>.jsonl`. State/cursors live
in `data/state/`.

### Tools watchlist & curated lists

Edit `config/github_watch.yaml`:

- `watchlist.accounts` — orgs/users whose new non-fork repos are tracked (`track_all: true`
  keeps historical repos too; used for company-affiliated accounts).
- `curated_lists` — generic list of GitHub repos whose markdown/data files are re-parsed
  each run (e.g. [awesome-bof](https://github.com/chryzsh/awesome-bof) catalog,
  [Cobalt Strike Community Kit](https://github.com/Cobalt-Strike/community_kit)
  `tracked_repos.txt`). Linked repos are always included (no star threshold); owners are
  verified and added to a dynamic watch extension.
- `discovery` — GitHub search queries with a configurable star floor (default ≥5).

Quality filters skip forks, archived empty junk, and basic lure/spam description patterns.

## Enabling GitHub Pages

After pushing this repo to `github.com/buffalotraceroute/secblog`:

1. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. Ensure Actions are allowed to write (Settings → Actions → General → Workflow permissions:
   Read and write).
3. Run the **Collect and deploy** workflow (`workflow_dispatch`) once, or push to `main`.
4. Site URL: `https://buffalotraceroute.github.io/secblog/`

The workflow builds `dist/` and deploys with `actions/upload-pages-artifact` +
`actions/deploy-pages`. Collected data is committed back via
`stefanzweifel/git-auto-commit-action`.

## Content policy

- Public pages show title, author, date, blog, category, tags, and a short feed excerpt
  (HTML stripped, ≤ ~300 chars).
- Every card links to the **original URL** and a **Wayback** snapshot.
- Full article bodies are never stored in this repository or published on the site.
- Dead / spam-hijacked domains are archived via Wayback only.
- The Tools section stores GitHub **metadata + permalinks only**; tool code/binaries are
  never downloaded, hosted, or mirrored.
- Excerpts and titles remain copyright of their authors; this is a discovery index.

## License

Code and configuration: MIT. Aggregated metadata/excerpts remain copyright of original authors.
