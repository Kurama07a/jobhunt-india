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

## The three sources (ATS vendors)

`upstream.SOURCES` defines one adapter per vendor:

| ATS | Board API endpoint (`{slug}` = company identifier) | Jobs array | Full description |
|---|---|---|---|
| **Ashby** | `https://api.ashbyhq.com/posting-api/job-board/{slug}` | `payload["jobs"]` | included in list response |
| **Greenhouse** | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` | `payload["jobs"]` | **not** in list response — needs a per‑job call with `?content=true` |
| **Lever** | `https://api.lever.co/v0/postings/{slug}?mode=json` | the response *is* the array | included in list response |

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

### Greenhouse selective description fetch

Greenhouse's list endpoint omits descriptions. In `_fetch_board()`
(`app/ingestion.py:222`), JobHunt India does a **preliminary** software+India check using
only the title/department/team/location. **Only if both look promising** does it make the
extra call:

```
GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{source_job_id}?content=true
```

via `_fetch_greenhouse_description()`. This keeps the expensive per‑job fetch off the vast
majority of non‑matching Greenhouse postings.

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

`recent` mode ("~4 minutes") restricts Wayback to the last 30 days; full discovery
("~26 minutes") crawls the entire index.

### Seed boards

`ensure_seed_boards()` runs on **every** startup and **every** ingestion run, regardless
of mode:

- `<job-boards>/boards.seed.json` — the upstream's small curated list, so a fresh install
  has boards to check without touching any archive. `discovered_via = "upstream_seed"`.
- `data/india-boards.seed.json` — JobHunt India's own curated Indian‑company slugs.
  `discovered_via = "india_seed"`. Current contents:
  - greenhouse: `groww`, `postman`, `razorpaysoftwareprivatelimited`, `slice`
  - lever: `cred`, `meesho`, `mindtickle`

Boards imported from any seed are matched against `INDIAN_COMPANY_HINTS`
(`app/classifier.py:105`) so recognised Indian companies get `is_india_company = true`
immediately. During scans, `is_india_company` is sticky — it is only ever OR‑ed to `true`,
never cleared.

---

## Fetching a board

`_fetch_board(board, use_etag)` (`app/ingestion.py:163`) for one board:

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
