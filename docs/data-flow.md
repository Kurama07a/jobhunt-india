# Data Flow — one ingestion run, end to end

This document traces a single ingestion run from trigger to stored rows. Function
references point at `app/ingestion.py` unless noted.

## Trigger → run record → background thread

```mermaid
sequenceDiagram
    participant N as n8n
    participant API as POST /api/admin/ingest
    participant DB as PostgreSQL
    participant T as Background thread

    N->>API: {mode} + X-Ingest-Token
    API->>API: _verify_admin(token)  (hmac.compare_digest)
    API->>DB: create_or_get_run(mode)
    alt a run is already queued/running
        DB-->>API: existing run_id, created=false
        API-->>N: 202 {status: "already_running"}
    else no active run
        DB-->>API: new run_id (status=queued), created=true
        API->>T: launch_ingestion(run_id, mode)  (daemon thread)
        API-->>N: 202 {run_id, status: "queued"}
    end
```

- **Auth**: `X-Ingest-Token` header compared to `INGEST_TOKEN` with
  `hmac.compare_digest` (`app/main.py:75`). Missing/wrong → `401`.
- **Mode** (`IngestionRequest`): `incremental` (default), `refresh_recent`,
  `full_discovery`. The CLI additionally allows `smoke`. Validated against
  `ALLOWED_MODES` in `create_or_get_run`.
- **Single‑flight**: `create_or_get_run` does `SELECT id FROM ingestion_runs WHERE status
  IN ('queued','running') … FOR UPDATE`. If a row exists, the same `run_id` is returned
  and **no** new thread starts.

## The sweep

```mermaid
flowchart TD
    START["run_ingestion(run_id, mode, limit_per_ats)"] --> LOCK{"pg_try_advisory_lock(4912024091)?"}
    LOCK -- no --> FAILLOCK["run -> failed\n'another ingestion holds the database lock'"]
    LOCK -- yes --> RUNNING["run -> running, started_at = now()"]
    RUNNING --> SEED["ensure_seed_boards()\nupstream boards.seed.json + data/india-boards.seed.json"]
    SEED --> DISC{"mode in {refresh_recent, full_discovery}?"}
    DISC -- yes --> REFRESH["refresh_boards(mode)\n= write DB boards to cache\n+ upstream.load_boards(refresh=True)  (Wayback/urlscan)\n+ sources.discover_indian_boards()  (directed slug probing, all ATSes)\n+ full_discovery only: _resurrect_dead_boards()"]
    DISC -- no --> SKIPD[" "]
    REFRESH --> LOADB
    SKIPD --> LOADB["SELECT active boards\nORDER BY ats, lower(slug)"]
    LOADB --> LIMIT{"limit_per_ats or smoke?"}
    LIMIT -- yes --> CAP["keep first N boards per ATS"]
    LIMIT -- no --> NOCAP[" "]
    CAP --> POOL
    NOCAP --> POOL["ThreadPoolExecutor(max_workers <= 8)"]
    POOL --> FB["_fetch_board(board, use_etag) per board"]
    FB --> PERSIST["_persist_board_result(result)\n(own transaction per board)"]
    PERSIST --> TICK{"every 25 boards"}
    TICK -- yes --> UPD["_update_run(run_id, counters)"]
    TICK -- no --> CONT[" "]
    UPD --> DONE
    CONT --> DONE{"all boards done?"}
    DONE -- yes --> POST["post-sweep:\n_promote_india_companies()  (every run)\nfull_discovery only: _close_stale_jobs()  (>120d)"]
    POST --> COMPLETE["run -> completed, finished_at = now(), error = NULL"]
    POOL -. any unhandled exception .-> FAILRUN["run -> failed, error = <exc>"]
    COMPLETE --> UNLOCK["pg_advisory_unlock(4912024091)"]
    FAILRUN --> UNLOCK
    FAILLOCK --> ENDX["return"]
```

Key points:

- **Board ordering** is deterministic (`ats, lower(slug)`).
- **`use_etag = mode not in {"full_discovery", "smoke"}`.** Full/smoke re‑download every
  board so classifier changes apply everywhere.
- **`limit_per_ats`**: set by the CLI `--limit-per-ats`, or defaulted to `2` for
  `smoke`. It keeps only the first N boards per ATS — a bounded live test.
- **Counters** accumulated across boards: `checked`, `succeeded`, `failed`, `unchanged`,
  `seen`, `targeted`, `upserted`, `closed`. Flushed to `ingestion_runs` every
  `PROGRESS_INTERVAL = 25` boards and once at the end.
- **Worker exceptions** are caught per‑future and converted to an `error` result so one
  bad board never aborts the sweep. An exception *outside* the loop marks the whole run
  `failed`.

## Per‑posting transformation (inside `_fetch_board`)

```mermaid
flowchart LR
    RAW["vendor JSON posting"] --> NORM["SOURCES[ats].normalize(job)"]
    NORM --> CLEAN["upstream._clean()\ncollapse whitespace"]
    CLEAN --> PT["plain_text(description)\nstrip tags, unescape entities, drop NUL"]
    PT --> PRELIM{"greenhouse AND\nprelim software AND prelim india?"}
    PRELIM -- yes --> GHDESC["GET .../jobs/{id}?content=true\nreplace description"]
    PRELIM -- no --> SKIPGH[" "]
    GHDESC --> CLASSIFY
    SKIPGH --> CLASSIFY["classify_job(title, description, location,\nboard_slug, board_is_india, is_remote, dept, team)"]
    CLASSIFY --> TARGET{"classification.is_target\n(software AND india)?"}
    TARGET -- no --> DROP["discard"]
    TARGET -- yes --> ROW["build row + content_hash(row minus raw_metadata)"]
    ROW --> COLLECT["target_rows[]"]
```

The row assembled for each target posting carries: identity (`ats`, `source_job_id`,
`board_slug`), display fields (`company`, `title`, `department`, `team`,
`employment_type`, `location`, `city`, `is_remote`, `workplace_type`, `published_at`),
`description` (≤ 60,000 chars) + `description_excerpt` (~420 chars), `apply_url`, and
classifier output (`india_match_reason`, `experience_min/max/level`,
`experience_is_explicit`, `entry_level_score`, `skills[]`,
`salary_min/max/currency/period`). `raw_metadata` (jsonb) keeps department/team/
workplace_type/source published string. `content_hash` is a SHA‑256 over the row minus
`raw_metadata` — stored for change detection/debugging.

## Persisting one board result (`_persist_board_result`)

Each board is persisted in **its own transaction**. Behaviour by `status`:

| `status` | `job_boards` effect | `jobs` effect | Counters |
|---|---|---|---|
| `unchanged` (HTTP 304) | `last_checked_at`, `last_success_at` = now; `consecutive_failures = 0`; `last_error = NULL` | none | `succeeded += 1`, `unchanged += 1` |
| `dead` (HTTP 404) | `is_active = false`; `last_error` set; `consecutive_failures += 1` | **all** active jobs for this board → `is_active = false`, `closed_at = now()` | `failed += 1`, `closed += <n>` |
| `error` (network/parse) | `last_checked_at` = now; `last_error` set; `consecutive_failures += 1` | **none** — failures never close jobs | `failed += 1` |
| `modified` (HTTP 200) | `is_active = true`; `is_india_company |= result flag`; `etag = COALESCE(new, old)`; success timestamps; `consecutive_failures = 0`; `jobs_seen = <n>` | upsert every `target_row` (`UPSERT_JOB_SQL`); then close any active job for this board whose `source_job_id` is **not** in this response | `succeeded += 1`, `seen += <normalized>`, `targeted += <rows>`, `upserted += <rows>`, `closed += <omitted>` |

### The upsert (`UPSERT_JOB_SQL`, `app/ingestion.py:291`)

`INSERT … ON CONFLICT (ats, source_job_id) DO UPDATE`:

- Refreshes all display + classifier columns from the new data.
- `published_at = COALESCE(excluded.published_at, jobs.published_at)` — never lose a known
  timestamp.
- `last_seen_at = now()`, `closed_at = NULL`, `is_active = true` — re‑seen jobs are
  revived.
- `description` / `description_excerpt` keep the previous value if the new one is empty
  (protects against a board response that drops descriptions).
- `first_seen_at` is set once by the column default and never overwritten.

## Closing / lifecycle rules

```mermaid
stateDiagram-v2
    [*] --> Active: first seen in a modified board response
    Active --> Active: seen again -> last_seen_at = now()
    Active --> Closed: successful board response omits it
    Active --> Closed: board returns 404 (dead)
    Active --> Closed: reclassify decides it is no longer a target
    Active --> Closed: full_discovery + published_at older than 120d (_close_stale_jobs)
    Closed --> Active: seen again in a later successful response (closed_at -> NULL)
```

**Only a successful, exhaustive response closes a job.** Network errors and parse errors
leave jobs untouched — the guiding principle is *never close on uncertainty*. The
120‑day stale close is the one exception, and runs only on the monthly unconditional
sweep where every live posting was just re‑confirmed.

## Terminal state & what the dashboard reads

At the end, `ingestion_runs` has the full tally plus `status = completed | failed`,
`finished_at`, and `error`. The dashboard polls `/api/sync-status` (latest run) and
`/api/stats` (live counts + latest run summary); it never sees boards or the sweep
directly. See [api.md](api.md).
