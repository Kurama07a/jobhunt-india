# JobHunt India

An entry-first, PostgreSQL-backed software job feed for India. The production site is
designed for `https://jobhunt.prakhar.wtf` and is refreshed by n8n.

## What it does

- Reuses Matt Herzog's MIT-licensed [`mherzog4/job-boards`](https://github.com/mherzog4/job-boards)
  discovery and public ATS adapters, pinned to commit
  `da7885cff552c513319318f2f31ed23f049f426e`.
- Discovers and checks public Ashby, Greenhouse, and Lever company boards with a fixed
  maximum concurrency of 8 and an identifiable `JOB_SCRAPER_CONTACT` user agent.
- Keeps India-located software roles plus software roles from recognized Indian-company
  boards. The database, not the browser, owns classification and lifecycle state.
- Extracts explicit experience ranges, infers experience level, assigns an early-career
  score, identifies common engineering skills, and parses salary ranges when published.
- Marks roles inactive only after a successful exhaustive board response omits them.
  Failed board requests never close jobs. ETags skip byte-identical boards on incremental
  runs.
- Provides an accessible responsive dashboard with filters for search, posting age,
  experience level, maximum years required, remote status, company, location, skills,
  ATS source, employment type, and sort order.

## Runtime architecture

```text
n8n schedule/manual trigger
  -> authenticated async ingestion API
  -> upstream board discovery + ATS public APIs (8 workers maximum)
  -> India/software classification and experience extraction
  -> PostgreSQL upsert + posting lifecycle
  -> FastAPI JSON API
  -> same-origin responsive web dashboard
```

The workflow runs:

- every 4 hours: conditional incremental sweep of all known boards;
- daily at 02:20 Europe/Berlin: recent Wayback + urlscan board discovery, then sweep;
- monthly on day 1 at 03:10 Europe/Berlin: full archive discovery and unconditional sweep.

The workflow polls its ingestion run to completion and fails visibly if the application
reports a failed run. PostgreSQL advisory locking prevents concurrent sweeps.

## Required environment

```dotenv
DATABASE_URL=postgresql://jobhunt:password@postgres-host:5432/jobhunt
INGEST_TOKEN=long-random-secret
JOB_SCRAPER_CONTACT=operator@example.com
ALLOWED_HOSTS=jobhunt.prakhar.wtf,localhost,127.0.0.1
SCRAPER_CONCURRENCY=8
APP_ENV=production
PORT=8000
```

`SCRAPER_CONCURRENCY` is capped at 8 in code to preserve the upstream project's
good-citizen traffic contract.

## Local checks

```bash
python -m pytest -q
docker build -t jobhunt-india .
```

With PostgreSQL available, initialize and run a bounded live smoke test:

```bash
python -m app.cli init-db
python -m app.cli ingest --mode smoke --limit-per-ats 2
```

## API surface

- `GET /health` — database and feed health
- `GET /api/stats` — dashboard counts and last run
- `GET /api/filters` — available companies, locations, skills, and sources
- `GET /api/jobs` — paginated filterable job feed
- `GET /api/jobs/{id}` — full job details
- `GET /api/sync-status` — safe public sync progress
- `POST /api/admin/ingest` — token-protected ingestion start
- `GET /api/admin/runs/{id}` — token-protected ingestion run status

## Attribution

The upstream `job-boards` source is copied into the production image from its pinned
MIT-licensed commit. Its own `LICENSE` remains in `/opt/job-boards/LICENSE` inside the
image. Job descriptions and application URLs remain attributed to the publishing
companies and ATS endpoints; this service stores only public recruitment data.

