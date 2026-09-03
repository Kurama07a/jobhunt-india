# Operations & Local Development

## Local setup

Requirements: Python 3.12, PostgreSQL, and a checkout of the upstream `job-boards`
project.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt          # includes runtime deps + pytest

# vendored discovery/adapter module — either:
git clone https://github.com/mherzog4/job-boards.git ../job-boards-upstream
git -C ../job-boards-upstream checkout da7885cff552c513319318f2f31ed23f049f426e
# ...or set JOB_BOARDS_PATH to an existing checkout.

export DATABASE_URL=postgresql://jobhunt:password@localhost:5432/jobhunt
export INGEST_TOKEN=dev-token
export JOB_SCRAPER_CONTACT=you@example.com
```

`app/ingestion.py:_load_upstream` looks for `../job-boards-upstream/job_boards.py`
relative to the repo, or `$JOB_BOARDS_PATH`.

### Run the app

```bash
uvicorn app.main:app --reload --port 8000
# dashboard: http://localhost:8000/   API: http://localhost:8000/api/jobs
```

Startup applies `schema.sql`, cleans up stale runs, and seeds boards automatically.

### Tests

```bash
python -m pytest -q
```

`tests/test_classifier.py` — classifier behaviour (India/remote gating, software
detection, experience/skill/salary parsing, display names).
`tests/test_assets.py` — the n8n workflow JSON is valid and has the production schedules;
the dashboard `<head>` pulls no `https://` assets; the schema keeps its unique key and
filter indexes.

CI also runs `python -m compileall -q app`, `node --check app/static/app.js`, and
`docker build`.

---

## Operations CLI — `python -m app.cli <command>`

Opens the pool (applies the schema), runs, closes the pool.

| Command | What it does |
|---|---|
| `init-db` | Applies `schema.sql` (done implicitly by opening the pool) and prints `database initialized`. Safe to re‑run. |
| `ingest [--mode MODE] [--limit-per-ats N]` | Runs a sweep **synchronously in the foreground**. `MODE` ∈ `incremental` (default), `refresh_recent`, `full_discovery`, `smoke`. Errors out if a run is already active. |
| `reclassify` | Re‑runs the classifier over stored active jobs, no network. Prints `reclassified <scanned> jobs; updated <n>; closed <n>`. Run after editing `app/classifier.py`. |
| `import-boards PATH [--via LABEL]` | Imports a `{ "ashby": [...], "greenhouse": [...], "lever": [...] }` JSON file into `job_boards`. `--via` sets `discovered_via` (default `bootstrap_import`). |

### Bounded live smoke test

```bash
python -m app.cli init-db
python -m app.cli ingest --mode smoke --limit-per-ats 2   # 2 boards per ATS, ETags off
python -m app.cli reclassify
```

`smoke` mode: `limit_per_ats` defaults to 2, ETags are disabled, no board discovery.
Ideal for a first‑run sanity check against real ATS APIs without a 13k‑board sweep.

### Modes at a glance

| Mode | Board discovery | ETags (`If-None-Match`) | Typical trigger |
|---|---|---|---|
| `incremental` | no | yes | n8n every 4h / `Run Now` |
| `refresh_recent` | recent (Wayback ≤30d + urlscan.io) | yes | n8n daily 02:20 |
| `full_discovery` | full archive crawl | **no** (re‑downloads all) | n8n monthly, day 1 03:10 |
| `smoke` | no | **no** | CLI only |

---

## Runbook

### "The feed looks stale"
1. `GET /api/sync-status` — check `status` and `finished_at` of the latest run.
2. If `status = "failed"`: `GET /api/admin/runs/{id}` (with `X-Ingest-Token`) for
   `error`.
3. Check the n8n execution history for *"JobHunt India — Discover & Ingest Jobs"*.
4. Re‑trigger: `curl -XPOST https://jobhunt.prakhar.wtf/api/admin/ingest -H
   "X-Ingest-Token: $INGEST_TOKEN" -H 'Content-Type: application/json' -d
   '{"mode":"incremental"}'` — or n8n's `Run Now`.

### "Ingestion won't start / stuck"
- `POST /api/admin/ingest` returning `already_running` means a run is `queued`/`running`.
  Check `/api/admin/runs/{id}`.
- A run stuck `running` for > 2h is auto‑failed on the **next app restart** (lifespan
  cleanup). Otherwise, restart the container.
- `error = "another ingestion holds the database lock"` → a previous process still holds
  advisory lock `4912024091`; it releases when that DB connection dies (restart clears
  it).

### "A whole company disappeared from the feed"
- The board likely returned `404` and was marked `dead` (`job_boards.is_active = false`),
  which closed all its jobs. Check `job_boards.last_error` / `consecutive_failures`.
- If the slug changed, add the new slug to `data/india-boards.seed.json` (or
  `import-boards`) and run an ingestion. The next `refresh_recent`/`full_discovery` may
  also rediscover it.

### "Classifier change didn't take effect"
- Existing rows aren't re‑evaluated by an `incremental` sweep unless the board changed
  (ETag) and the posting is re‑upserted. Run `python -m app.cli reclassify`, or trigger
  `full_discovery` (which ignores ETags and re‑classifies everything).

### "Deploy failed at 'wrong commit deployed'"
- Coolify finished a build for a SHA other than the one CI expected. Re‑run the GitHub
  Actions `deploy` job, or push a fresh commit. Confirm Coolify auto‑deploy is still
  disabled (only the n8n bridge should deploy).

### Health
- `GET /health` → `503` means the DB is unreachable from the container. Check
  `DATABASE_URL`, network, and PostgreSQL.
- The container `HEALTHCHECK` hits `/health` every 30s; Coolify restarts an unhealthy
  container.

---

## Safe manual SQL

```sql
-- latest runs
SELECT id, mode, status, requested_at, finished_at, boards_failed, jobs_upserted, error
FROM ingestion_runs ORDER BY requested_at DESC LIMIT 10;

-- boards failing repeatedly
SELECT ats, slug, consecutive_failures, last_error, last_checked_at
FROM job_boards WHERE consecutive_failures > 0 ORDER BY consecutive_failures DESC;

-- re-open a board that was wrongly marked dead (then run an ingestion)
UPDATE job_boards SET is_active = true, consecutive_failures = 0, last_error = NULL
WHERE ats = 'greenhouse' AND slug = 'somecompany';

-- prune very old closed jobs (no automatic retention)
DELETE FROM jobs WHERE NOT is_active AND closed_at < now() - interval '180 days';
```
