# Where the jobs come from

Every job in JobHunt India originates from a **public ATS job‑board API** operated by one
of three vendors. JobHunt India never scrapes HTML career pages and never uses a paid
aggregator. The pipeline has two halves:

1. **Discovery** — building and maintaining the list of company boards to check.
2. **Fetch** — pulling each board's current postings from its vendor JSON API.

Both halves are implemented by the vendored **`mherzog4/job-boards`** project (MIT
licensed), pinned to commit `da7885cff552c513319318f2f31ed23f049f426e`. JobHunt India
imports it as a module (`app/ingestion.py:_load_upstream`) and calls into it; it does not
fork or modify it.

---

## The four sources (ATS vendors)

`upstream.SOURCES` defines an adapter for the first three; `app/sources.py`
(`EXTRA_ATS`) adds SmartRecruiters natively. `ingestion.ALL_ATS` is the union and is
what every board-grouping loop iterates.

| ATS | Board API endpoint (`{slug}` = company identifier) | Jobs array | Full description |
|---|---|---|---|
| **Ashby** | `https://api.ashbyhq.com/posting-api/job-board/{slug}` | `payload["jobs"]` | included in list response |
| **Greenhouse** | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` | `payload["jobs"]` | **not** in list response — needs a per‑job call with `?content=true` |
| **Lever** | `https://api.lever.co/v0/postings/{slug}?mode=json` | the response *is* the array | included in list response |
| **SmartRecruiters** | `https://api.smartrecruiters.com/v1/companies/{slug}/postings?country=in` | `payload["content"]`, paginated by `offset` (100/page, capped at 1500) | **not** in list response — needs `GET .../postings/{id}` → `jobAd.sections.*.text` |
| **Workable** | `https://jobs.workable.com/api/v1/jobs?location=india` — one shared feed, **not** per company | `payload["jobs"]`, paginated by `nextPageToken` (20/page, ~190 pages) | **inline** in the feed (`description`, full HTML) |

SmartRecruiters is fetched India-only (`country=in`) — a global board like `BoschGroup`
drops from ~4,800 postings to a few hundred. An unknown company slug returns HTTP 200
with `totalFound: 0`, which the adapter treats as an exhaustive empty response (board
kept, its jobs closed).

**Workable is a feed, not a set of boards.** It is an embedded widget, so per-company
boards are invisible to web archives and the widget endpoint is dead for most accounts.
Instead `app/sources.py` pulls the whole `jobs.workable.com` India marketplace feed once
per discovery run (`load_workable_feed`, ~3 min sequential with a 0.4 s inter-page
delay — `jobs.workable.com` rate-limits bursts), groups jobs by company, and registers a
`job_boards` row per company (`_prime_feed_sources`). Each Workable "board" is then
served from that in-process cache with **no** per-board HTTP. A pull that does not reach
the last page is discarded (`_workable_feed_ok` stays False) and every Workable board
reports `unchanged` that sweep, so a rate-limited feed can never close a company's jobs.

See [coverage-analysis.md](coverage-analysis.md) for why Recruitee / Keka / Freshteam /
Zoho Recruit were evaluated and deferred.

Each adapter provides:

- `domains` — the public hostnames where that vendor hosts boards (used by discovery to
  recognise board URLs found in web archives).
- `api` — the endpoint template above.
- `jobs` — a lambda that extracts the postings array from the parsed payload.
- `normalize` — `normalize_ashby` / `normalize_greenhouse` / `normalize_lever`, which map
  vendor‑specific JSON into a common field set and return `None` for postings that should
  be dropped. The normalized shape used downstream includes: `id`, `title`, `department`,
  `team`, `employmentType`, `location`, `isRemote`, `workplaceType`, `publishedAt`,
  `jobUrl`, and `_description` (raw HTML/text description, later stripped to plain text).
- `content_param` — only Greenhouse (`content=true`), which the upstream notes costs
  "~26x the bytes", so it is fetched selectively (see below).
- `junk_prefixes` — internal path prefixes to ignore during slug discovery (e.g. Ashby's
  `root.`).

### Selective description fetch (Greenhouse & SmartRecruiters)

Both list endpoints omit descriptions. In `_fetch_board()` JobHunt India runs a
**preliminary** software check on title/department/team, then makes the extra call
**only when** the posting is prelim‑software **and** (India‑linked **or** remote — the
remote‑eligibility check needs the body text). `_detail_description()` dispatches to
`_fetch_greenhouse_description()` (`…/jobs/{id}?content=true`) or, for extra‑ATS
platforms, `sources.describe()` (SmartRecruiters `…/postings/{id}` → `jobAd.sections`).

The earlier gate also required the *India* signal before fetching, which made the
remote‑from‑India path impossible on Greenhouse (the check needs the description that the
gate withheld). Adding "or remote" fixed that — see
[coverage-analysis.md](coverage-analysis.md) Tier 2.

---

## Board discovery

`refresh_boards(mode)` (`app/ingestion.py:136`) runs only for modes `refresh_recent` and
`full_discovery`:

1. **Seed the upstream cache from the DB.** `_write_upstream_cache_from_db()` writes every
   active `job_boards` row into `boards.json` next to the upstream module, so discovery
   *adds to* — never replaces — the boards JobHunt India already knows.
2. **Call `upstream.load_boards(refresh=True, ats_list=[ashby, greenhouse, lever],
   concurrency ≤ 8, recent=<mode == refresh_recent>)`.** Upstream discovery:
   - **Wayback Machine CDX index** (`web.archive.org`) — primary source. Queries for URLs
     under each vendor's `domains` and extracts the `{slug}` segment.
   - **urlscan.io** — supplementary, used only when `recent=True`. Catches boards created
     in roughly the last `RECENT_WINDOW_DAYS = 30` days that the archive hasn't captured
     yet.
   - **Common Crawl** (`index.commoncrawl.org`) — fallback if Wayback fails.
   - Every candidate slug is shape‑checked by `plausible(slug, ats)` (regex
     `^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$`, rejects UUIDs and junk like `api`, `static`,
     `assets`, `favicon.ico`, …) and then validated with a cheap `HEAD`/`GET` (200 =
     keep, 404 = drop).
   - Results are merged with `boards.seed.json` + the cache and written back to
     `boards.json`.
3. **Import the returned payload** via `import_boards_payload(payload,
   "recent_discovery" | "full_discovery")` — an idempotent upsert into `job_boards`
   (`ON CONFLICT (ats, slug) DO UPDATE`). The function returns the net number of new
   board rows, recorded as `ingestion_runs.boards_discovered`.
4. **Directed discovery** (`sources.discover_indian_boards`) — probe each **per‑slug**
   ATS endpoint (upstream three + SmartRecruiters; Workable is feed‑based and excluded)
   with slug candidates generated from `data/indian-companies.json`. Slug guesses that
   resolve (HEAD 200 for the upstream three; `totalFound > 0` for SmartRecruiters) are
   imported as `discovered_via = "directed_india"`.
5. **Dead-board resurrection** (`full_discovery` only) — every `is_active = false`
   board is re‑probed; one that now resolves is reactivated with
   `last_discovered_at = now()`. A transient 404 no longer removes a company forever.

`recent` mode ("~4 minutes") restricts Wayback to the last 30 days; full discovery
("~26 minutes") crawls the entire index. Directed discovery adds ~2–4 minutes.

### `is_india_company` auto-promotion

After every sweep, `_promote_india_companies()` flags any board with **≥3** active
`india_location` postings (or **≥40%** of ≥2). A flagged board also contributes its
geography‑neutral *remote* roles. This recovers openings from India‑heavy companies the
bootstrap list never named (InMobi, Glance, Turing, Zeta, Porter, …). The flag is
sticky — never cleared here.

### Seed boards

`ensure_seed_boards()` runs on **every** startup and **every** ingestion run, regardless
of mode:

- `<job-boards>/boards.seed.json` — the upstream's small curated list, so a fresh install
  has boards to check without touching any archive. `discovered_via = "upstream_seed"`.
- `data/india-boards.seed.json` — JobHunt India's own curated slugs, keyed by ATS
  (`ashby`, `greenhouse`, `lever`, `smartrecruiters`). `discovered_via = "india_seed"`.

Boards imported from any seed are matched against the known‑Indian‑company set —
the built‑in bootstrap list in `app/classifier.py` **merged with the slug hints from
`data/indian-companies.json`** (~40 → ~430 normalized entries) — so recognised
companies get `is_india_company = true` immediately. During scans the flag is sticky:
only ever OR‑ed to `true`, never cleared.

---

## Fetching a board

`_fetch_board(board, use_etag)` calls `_board_records(board, use_etag)` to get the
normalized postings, then runs the shared classify‑and‑build loop. `_board_records`
branches by platform: an extra‑ATS board (SmartRecruiters) goes to
`sources.fetch_records` (pagination + `country=in`); the upstream three take the path
below. Both return the same shape — a terminal `{"status": "unchanged"|"dead"|"error"}`
or `{"status": "ok", "normalized": [...], "etag": …}`.

For the upstream three, per board:

1. `upstream.fetch(upstream.board_url(ats, slug), etag=<stored etag if use_etag>,
   meta=meta)`.
   - **Headers**: `User-Agent: job-boards-scraper/1.0 (public posting APIs; contact:
     $JOB_SCRAPER_CONTACT)`, `Accept-Encoding: gzip`, and `If-None-Match: <etag>` when an
     ETag is known.
   - **Retries/backoff**: up to 4 attempts; exponential backoff on 5xx; honours
     `Retry-After` on 429/403.
   - **Outcomes / exceptions**:
     - `NotModified` (HTTP 304) → result `status = "unchanged"` — board skipped entirely.
     - `NotFound` (HTTP 404) → result `status = "dead"`.
     - any other exception → result `status = "error"` with the exception text.
   - On success `meta["etag"]` holds the new ETag for next time.
2. Parse JSON, extract the jobs array via the adapter's `jobs` lambda, `normalize` each
   posting, `upstream._clean` it (collapse whitespace on all string fields except the
   description).
3. For each normalized posting, run the [classifier](classification.md). Non‑matching
   postings are discarded. Matching ones become `target_rows` with a `content_hash`.
4. Return `status = "modified"` with `target_rows`, `jobs_seen` (count of normalized
   postings), and the new `etag`.

### ETag conditional requests

`use_etag = mode not in {"full_discovery", "smoke"}`. So incremental and
`refresh_recent` runs send `If-None-Match`; a `304` short‑circuits the whole board
(counted as `boards_unchanged`). Full‑discovery and smoke runs always re‑download so a
classifier change can be applied to every board.

---

## Good‑citizen contract

Carried over from the upstream project and enforced here:

| Rule | Where |
|---|---|
| Concurrency ≤ 8 board fetches at once | `SCRAPER_CONCURRENCY` clamped to `1..8` in `app/config.py:35`; passed as `max_workers` |
| Identifiable contact in `User-Agent` | `JOB_SCRAPER_CONTACT` env var, required by upstream `fetch()` |
| Skip unchanged boards | ETag `If-None-Match` on incremental runs |
| Public APIs only | `upstream.SOURCES` adapters; no HTML scraping |
| Attribution preserved | upstream `LICENSE` shipped at `/opt/job-boards/LICENSE`; job descriptions and apply URLs stay attributed to the publishing company / ATS |
| Pinned upstream | Dockerfile `ARG JOB_BOARDS_COMMIT=da7885cff552c513319318f2f31ed23f049f426e` |

---

## What is stored vs. discarded

- **Stored**: public recruitment metadata only — company, title, department/team,
  location, remote flags, published timestamp, apply URL, plain‑text description (capped
  at 60,000 chars), and everything the classifier derives.
- **Discarded immediately**: any posting that is not a software role, or not
  India‑relevant; the raw HTML markup of descriptions (converted to plain text before
  storage); the full `boards.json` crawl output (gitignored; lives only inside the
  container / next to the upstream module).
