# Database

PostgreSQL is the single source of truth. The full schema is `app/schema.sql`, applied
idempotently on every process start by `open_pool()` (`app/db.py:24`). Extensions:
`pgcrypto` (for `gen_random_uuid()`) and `pg_trgm` (trigram indexes).

`schema.sql` ends with an idempotent migration block (`schema_version` is now `2`): a
`DO $$ … $$` that widens the `ats` CHECK constraints on `job_boards` and `jobs` to
include `smartrecruiters` (a bare `CREATE TABLE IF NOT EXISTS` never alters an existing
table), and `ALTER TABLE … ADD COLUMN IF NOT EXISTS last_discovered_at`. Safe to run on
every boot; validated against the production DB inside a rolled-back transaction.

Connection pool (`app/db.py`): `psycopg_pool.ConnectionPool`, `min_size=1`,
`max_size=12`, `timeout=20s`, `autocommit=True`, `row_factory=dict_row`.

---

## `schema_meta`

Key/value table. Currently holds `schema_version = '2'`. `/health` reads it to prove the
schema is present.

| Column | Type | Notes |
|---|---|---|
| `key` | `text` PK | |
| `value` | `text` | |
| `updated_at` | `timestamptz` | default `now()` |

---

## `job_boards`

One row per discovered company board. PK is `(ats, slug)`.

| Column | Type | Meaning |
|---|---|---|
| `ats` | `text` | `ashby` \| `greenhouse` \| `lever` \| `smartrecruiters` (CHECK) |
| `slug` | `text` | vendor board identifier (the `{slug}` in the API URL) |
| `display_name` | `text` | human name, from `display_company(slug)` |
| `is_india_company` | `bool` | recognised Indian company. **Sticky** — only ever OR‑ed to true |
| `discovered_via` | `text` | `upstream_seed` \| `india_seed` \| `recent_discovery` \| `full_discovery` \| `import` \| `bootstrap_import` \| `seed` |
| `is_active` | `bool` | false after a 404 (`dead`) |
| `etag` | `text` | last ETag; sent as `If-None-Match` on incremental runs |
| `last_checked_at` | `timestamptz` | any fetch attempt |
| `last_success_at` | `timestamptz` | last 200/304 |
| `last_error` | `text` | last failure text; cleared on success |
| `consecutive_failures` | `int` | incremented on error/dead, reset to 0 on success |
| `jobs_seen` | `int` | normalized postings in the last successful response |
| `last_discovered_at` | `timestamptz` \| null | last time a directed probe confirmed the slug (schema v2). Set when a dead board is resurrected. |
| `created_at` / `updated_at` | `timestamptz` | |

Indexes: `job_boards_active_idx (is_active, ats, slug)`,
`job_boards_india_idx (is_india_company) WHERE is_india_company`.

`discovered_via` also takes `directed_india` (slug-probe hit) since schema v2.

---

## `jobs`

One row per posting, keyed internally by a UUID `id`, uniquely by
`(ats, source_job_id)`.

### Identity & source

| Column | Type | Meaning |
|---|---|---|
| `id` | `uuid` PK | `gen_random_uuid()` |
| `ats` | `text` | CHECK in (`ashby`,`greenhouse`,`lever`) |
| `source_job_id` | `text` | vendor posting id; `UNIQUE (ats, source_job_id)` |
| `board_slug` | `text` | FK → `job_boards (ats, slug)` `ON UPDATE CASCADE` |
| `company` | `text` | `job_boards.display_name` or derived |

### Display

| Column | Type | Meaning |
|---|---|---|
| `title` | `text` | |
| `department`, `team` | `text` | default `''` |
| `employment_type` | `text` | vendor string (Full‑time, Internship, Contract, …) |
| `location` | `text` | raw vendor location string |
| `city` | `text` \| null | canonical Indian city from `extract_city` |
| `is_remote` | `bool` | from vendor `isRemote` |
| `workplace_type` | `text` | vendor `workplaceType` |
| `published_at` | `timestamptz` \| null | vendor publish time (parsed, UTC) |
| `description` | `text` | plain text, ≤ 60,000 chars |
| `description_excerpt` | `text` | ~420 chars, word‑boundary clipped |
| `apply_url` | `text` NOT NULL | official application URL |

### Lifecycle

| Column | Type | Meaning |
|---|---|---|
| `first_seen_at` | `timestamptz` | set once (column default), never overwritten |
| `last_seen_at` | `timestamptz` | bumped to `now()` on every upsert |
| `closed_at` | `timestamptz` \| null | when it was last closed |
| `is_active` | `bool` | feed visibility. Every read filters `is_active = true` |

### Classifier output

| Column | Type | Meaning |
|---|---|---|
| `india_match_reason` | `text` | `india_location` \| `remote_from_india` \| `indian_company` |
| `role_category` | `text` | default `'software engineering'` (reserved for future categories) |
| `experience_min`, `experience_max` | `numeric(4,1)` \| null | years |
| `experience_level` | `text` | CHECK in (`internship`,`entry`,`mid`,`senior`,`unknown`) |
| `experience_is_explicit` | `bool` | a number was actually parsed |
| `entry_level_score` | `smallint` | CHECK `0..100` |
| `skills` | `text[]` | canonical names, ≤ 12 |
| `salary_min`, `salary_max` | `numeric(14,2)` \| null | |
| `salary_currency` | `text` \| null | `INR` \| `USD` |
| `salary_period` | `text` \| null | `year` \| `month` \| `hour` |

### Bookkeeping

| Column | Type | Meaning |
|---|---|---|
| `content_hash` | `text` | SHA‑256 of the row (minus `raw_metadata`) at ingest |
| `raw_metadata` | `jsonb` | `{department, team, workplace_type, source_published_at}` |
| `created_at` / `updated_at` | `timestamptz` | |
| `search_document` | `tsvector` GENERATED STORED | `to_tsvector('english', title ‖ company ‖ location ‖ department ‖ team ‖ description_excerpt)` |

### Indexes

| Index | Definition | Serves |
|---|---|---|
| `jobs_source_unique` | `UNIQUE (ats, source_job_id)` | upsert conflict target |
| `jobs_active_recent_idx` | `(is_active, published_at DESC NULLS LAST)` | recency sort |
| `jobs_entry_recent_idx` | `(entry_level_score DESC, published_at DESC NULLS LAST) WHERE is_active` | default `sort=entry` |
| `jobs_level_idx` | `(experience_level, is_active)` | level chips |
| `jobs_company_idx` | `(company, is_active)` | company filter / sort |
| `jobs_location_trgm_idx` | GIN `gin_trgm_ops` on `location` | `location ILIKE '%…%'` |
| `jobs_title_trgm_idx` | GIN `gin_trgm_ops` on `title` | title `ILIKE` search |
| `jobs_skills_idx` | GIN on `skills` | `skills && ARRAY[…]` overlap |
| `jobs_search_idx` | GIN on `search_document` | `websearch_to_tsquery` full‑text |

---

## `ingestion_runs`

One row per triggered run. Drives `/api/stats`, `/api/sync-status`,
`/api/admin/runs/{id}`, and n8n's polling loop.

| Column | Type | Meaning |
|---|---|---|
| `id` | `uuid` PK | |
| `mode` | `text` | CHECK in (`incremental`,`refresh_recent`,`full_discovery`,`smoke`) |
| `status` | `text` | CHECK in (`queued`,`running`,`completed`,`failed`) |
| `requested_at` | `timestamptz` | default `now()` |
| `started_at` / `finished_at` | `timestamptz` \| null | |
| `boards_total` | `int` | boards selected for this sweep (after any per‑ATS cap) |
| `boards_checked` | `int` | boards processed so far |
| `boards_succeeded` | `int` | 200 + 304 |
| `boards_failed` | `int` | error + dead |
| `boards_unchanged` | `int` | 304 |
| `boards_discovered` | `int` | net new board rows from discovery |
| `jobs_seen` | `int` | normalized postings across all boards |
| `jobs_targeted` | `int` | postings that passed the classifier |
| `jobs_upserted` | `int` | rows written (currently == targeted) |
| `jobs_closed` | `int` | active jobs closed by omission / dead board |
| `error` | `text` \| null | failure detail |
| `metadata` | `jsonb` | reserved, default `{}` |

Index: `ingestion_runs_requested_idx (requested_at DESC)`.

Counters are flushed every 25 boards (`PROGRESS_INTERVAL`) so the dashboard progress bar
advances mid‑run.

---

## Concurrency & locking

| Guard | Purpose |
|---|---|
| `SELECT … FOR UPDATE` on active runs in `create_or_get_run` | at most one `queued`/`running` row |
| `pg_try_advisory_lock(4912024091)` for the whole sweep | at most one sweep across all processes; auto‑released if the connection dies |
| Per‑board transaction in `_persist_board_result` | a failure on board N doesn't roll back boards 1..N‑1 |
| Lifespan cleanup | runs stuck `queued`/`running` with `requested_at < now() - 2h` are forced to `failed` on startup |

## Retention

There is no automatic pruning. Closed jobs (`is_active = false`) and historical
`ingestion_runs` accumulate. All API reads filter `is_active = true`, so closed rows are
invisible to users but remain for audit/history. Prune manually if needed.
