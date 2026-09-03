# HTTP API Reference

Base URL (production): `https://jobhunt.prakhar.wtf`
Served by FastAPI (`app/main.py`). All `/api/*` responses carry `Cache-Control:
no-store`; `/static/*` carries `public, max-age=86400`. Every response gets the security
header set (CSP, `X-Frame-Options: DENY`, nosniff, `Referrer-Policy`,
`Permissions-Policy`). Interactive docs (`/docs`, `/redoc`) are **disabled**.

Two trust tiers:

- **Public** — no auth, safe for the browser: `/`, `/health`, `/api/stats`,
  `/api/filters`, `/api/jobs`, `/api/jobs/{id}`, `/api/sync-status`, `/robots.txt`.
- **Admin** — requires header `X-Ingest-Token: <INGEST_TOKEN>`, compared with
  `hmac.compare_digest`: `/api/admin/ingest`, `/api/admin/runs/{id}`,
  `/api/admin/coverage`.

---

## Public

### `GET /`  · `HEAD /`
Returns `static/index.html` (the dashboard). `HEAD` supported for uptime checks.

### `GET /robots.txt`
`User-agent: * / Allow: /`.

### `GET /health`
Liveness + schema/data sanity. Used by the Docker `HEALTHCHECK` and the CI smoke test.

`200`:
```json
{
  "status": "healthy",
  "database": "connected",
  "schema_version": "1",
  "active_boards": 812,
  "active_jobs": 1543
}
```
`503` (DB unreachable): `{"status":"unhealthy","database":"unavailable","detail":"<ExceptionType>"}`.

### `GET /api/stats`
Dashboard header metrics + a summary of the latest run.

```json
{
  "active_jobs": 1543,
  "posted_24h": 37,
  "entry_jobs": 610,
  "remote_jobs": 288,
  "newest_sync": "2026-09-03T09:12:44Z",
  "active_boards": 812,
  "indian_company_boards": 44,
  "checked_24h": 806,
  "latest_run": {
    "id": "…", "mode": "incremental", "status": "completed",
    "requested_at": "…", "started_at": "…", "finished_at": "…",
    "boards_total": 812, "boards_checked": 812, "boards_failed": 4,
    "boards_unchanged": 690, "jobs_seen": 40311, "jobs_targeted": 1550,
    "jobs_upserted": 1550
  }
}
```
`entry_jobs` counts `experience_level IN ('internship','entry')`. `posted_24h` uses
`COALESCE(published_at, first_seen_at)`.

### `GET /api/filters`
Facet values for the advanced filter UI. All counts are over `is_active` jobs.

```json
{
  "companies": [{"value": "Postman", "count": 22}, …],   // top 250 by count
  "locations": [{"value": "Bengaluru, India", "count": 190}, …], // top 180
  "skills":    [{"value": "Python", "count": 640}, …],    // top 80
  "sources":   [{"value": "greenhouse", "count": 900}, …],
  "levels":    ["internship", "entry", "unknown", "mid", "senior"]
}
```

### `GET /api/jobs`
The paginated, filterable feed. All parameters optional.

| Param | Type / bounds | Effect |
|---|---|---|
| `q` | string ≤ 120 | `websearch_to_tsquery` over `search_document`, plus `ILIKE` on title/company and a skill `ILIKE` match |
| `levels` | CSV of `internship,entry,mid,senior,unknown` | `experience_level = ANY(...)`; unknown value → `422` |
| `days` | int 1..3650 | `COALESCE(published_at, first_seen_at) >= now() - days` |
| `remote` | bool | `is_remote = <value>` |
| `ats` | CSV of `ashby,greenhouse,lever,smartrecruiters` | `ats = ANY(...)`; unknown value → `422` |
| `company` | string ≤ 180 | exact `company = <value>` |
| `location` | string ≤ 180 | `location ILIKE '%<value>%'` |
| `skills` | CSV ≤ 300 | `skills && ARRAY[...]` (overlap) |
| `employment_type` | string ≤ 120 | `employment_type ILIKE '%<value>%'` |
| `max_experience` | float 0..30 | `experience_is_explicit = true AND COALESCE(experience_min,0) <= <value>` — **inferred/unknown rows are excluded** by design |
| `explicit_experience` | bool | `experience_is_explicit = <value>` |
| `sort` | `entry` (default) \| `recent` \| `experience` \| `company` | see below |
| `page` | int 1..10000 (default 1) | |
| `page_size` | int 1..100 (default 24) | |

**Sort orders:**
- `entry`: `entry_level_score DESC, COALESCE(published_at, first_seen_at) DESC, title`
- `recent`: `COALESCE(published_at, first_seen_at) DESC, entry_level_score DESC`
- `experience`: `experience_min ASC NULLS LAST, entry_level_score DESC, date DESC`
- `company`: `lower(company), date DESC`

**Response:**
```json
{
  "jobs": [
    {
      "id": "uuid", "ats": "greenhouse", "source_job_id": "...",
      "company": "Postman", "title": "Software Engineer I",
      "department": "...", "team": "...", "employment_type": "Full time",
      "location": "Bengaluru, India", "city": "Bengaluru",
      "is_remote": false, "workplace_type": "Hybrid",
      "published_at": "...", "first_seen_at": "...", "last_seen_at": "...",
      "description_excerpt": "…", "apply_url": "https://…",
      "india_match_reason": "india_location",
      "experience_min": 0.0, "experience_max": 2.0,
      "experience_level": "entry", "experience_is_explicit": true,
      "entry_level_score": 90, "skills": ["Python", "PostgreSQL"],
      "salary_min": null, "salary_max": null,
      "salary_currency": null, "salary_period": null,
      "days_posted": 3
    }
  ],
  "pagination": { "page": 1, "page_size": 24, "total": 1543, "pages": 65 }
}
```
`total` comes from a `count(*) OVER()` window on the same query. `days_posted` is
computed in SQL from `COALESCE(published_at, first_seen_at)`. The list response returns
`description_excerpt`, not the full `description`.

### `GET /api/jobs/{job_id}`
`job_id` must be a UUID (else `422`). Returns one active job including the **full**
`description` (list view omits it). `404` if not found or not active.

### `GET /api/sync-status`
Public, safe view of the latest run for the dashboard's "Updated Nm ago" label and
progress bar.

```json
{
  "id": "…", "mode": "incremental", "status": "running",
  "requested_at": "…", "started_at": "…", "finished_at": null,
  "boards_total": 812, "boards_checked": 240, "boards_succeeded": 236,
  "boards_failed": 4, "boards_unchanged": 210, "boards_discovered": 0,
  "jobs_seen": 12010, "jobs_targeted": 470, "jobs_upserted": 470, "jobs_closed": 3
}
```
Before the first run ever: `{"status": "never_run"}`.

---

## Admin (token required)

### `POST /api/admin/ingest`
Starts (or joins) an ingestion run. Header `X-Ingest-Token` required.

Body:
```json
{ "mode": "incremental" }   // "incremental" | "refresh_recent" | "full_discovery"
```
(`mode` defaults to `incremental`. `smoke` is CLI‑only, not accepted here.)

`202 Accepted`:
```json
{ "run_id": "uuid", "status": "queued", "created": true, "mode": "incremental" }
```
If a run is already active, `created` is `false` and `status` is `already_running`, and
`run_id` is that existing run.

`401` if the token is missing/wrong.

### `GET /api/admin/coverage`
The discovery → classification funnel, for spotting where coverage leaks. Header
`X-Ingest-Token` required.

```json
{
  "boards": { "total": 13334, "active": 13314, "india_company": 8,
              "dead": 20, "failing": 22, "directed_confirmed": 0 },
  "boards_by_ats": [
    { "ats": "greenhouse", "boards": 6802, "india_company_boards": 4,
      "productive_boards": 346, "active_jobs": 1801 }, …
  ],
  "jobs": { "active": 2942, "early_career": 114, "remote": 571,
            "fresh_30d": 904, "older_90d": 1201, "median_age_days": 63.0 },
  "jobs_by_india_match_reason": [ { "reason": "india_location", "count": 2911 }, … ],
  "jobs_by_experience_level":  [ { "level": "senior", "count": 2023 }, … ],
  "recent_runs": [ { "mode": "incremental", "status": "completed",
                     "requested_at": "…", "boards_discovered": 0, "jobs_targeted": 422 }, … ]
}
```

`productive_boards` = distinct boards with ≥1 active job. `directed_confirmed` = boards
with `last_discovered_at` set. See [coverage-analysis.md](coverage-analysis.md).

### `GET /api/admin/runs/{run_id}`
Full run detail including `error` and `metadata` (the public `/api/sync-status` omits
these). `run_id` must be a UUID. `404` if unknown. This is the endpoint n8n polls.

```json
{
  "id": "…", "mode": "full_discovery", "status": "completed",
  "requested_at": "…", "started_at": "…", "finished_at": "…",
  "boards_total": 13480, "boards_checked": 13480, "boards_succeeded": 13120,
  "boards_failed": 360, "boards_unchanged": 0, "boards_discovered": 145,
  "jobs_seen": 902344, "jobs_targeted": 1712, "jobs_upserted": 1712,
  "jobs_closed": 240, "error": null, "metadata": {}
}
```

---

## Status codes

| Code | When |
|---|---|
| `200` | successful GET |
| `202` | ingestion accepted |
| `401` | admin endpoint, bad/missing token |
| `404` | unknown job id / run id |
| `422` | bad query param (non‑UUID id, unknown level/ats value, out‑of‑range number) |
| `400` | `Host` not in `ALLOWED_HOSTS` (TrustedHostMiddleware) |
| `503` | `/health` when the database is unreachable |
