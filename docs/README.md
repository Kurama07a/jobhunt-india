# JobHunt India — Documentation

JobHunt India is an entry-first, PostgreSQL-backed software‑engineering job feed for
India. It discovers public applicant‑tracking‑system (ATS) company boards, fetches their
job postings directly from public APIs, keeps only India‑relevant software roles,
enriches each role (experience, skills, salary, early‑career score), stores everything in
PostgreSQL, and serves a filterable JSON API plus a same‑origin web dashboard.

Production site: `https://jobhunt.prakhar.wtf`

---

## How it works in one paragraph

An **n8n** schedule (or a manual trigger) calls the token‑protected ingestion endpoint on
the **FastAPI** app. The app runs a background sweep: it optionally re‑discovers company
boards (via the vendored `mherzog4/job-boards` project, which mines the Wayback Machine
and urlscan.io), then fetches every known Ashby / Greenhouse / Lever board through their
public JSON APIs using at most 8 worker threads. Each posting is normalized and passed
through a rules‑based classifier that decides whether it is a *software* role that is
*India‑relevant*. Surviving rows are upserted into PostgreSQL; postings that a successful
exhaustive board response omitted are marked inactive. The dashboard and API read only
from PostgreSQL — the database, not the browser, owns classification and lifecycle state.

---

## Documentation map

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | Components, process model, runtime topology, design principles |
| [job-sources.md](job-sources.md) | Where jobs come from: upstream `job-boards`, ATS APIs, board discovery, ETags, good‑citizen contract |
| [data-flow.md](data-flow.md) | End‑to‑end flow of one ingestion run, step by step, with the board/job state machines |
| [classification.md](classification.md) | The India + software classifier, experience extraction, skills, salary, early‑career scoring |
| [database.md](database.md) | Schema, every table and column, indexes, upsert SQL, lifecycle rules, advisory locking |
| [api.md](api.md) | Full HTTP API reference: public endpoints, admin endpoints, query parameters, response shapes |
| [orchestration.md](orchestration.md) | n8n ingestion workflow, n8n CD bridge, schedules, polling loop, failure semantics |
| [deployment.md](deployment.md) | Dockerfile, GitHub Actions CI/CD, Coolify, environment variables, secrets |
| [frontend.md](frontend.md) | Dashboard structure, filter state, API calls, polling, security posture |
| [operations.md](operations.md) | Local development, the operations CLI, smoke tests, reclassification, runbook |

---

## Repository layout

```text
app/
  main.py         FastAPI application: routes, middleware, lifespan, admin auth
  ingestion.py    Sweep orchestrator: board discovery, fetch, persist, run bookkeeping
  classifier.py   Pure functions: India match, software match, experience/skills/salary
  config.py       Environment-driven Settings (frozen dataclass)
  db.py           psycopg connection pool + schema bootstrap
  schema.sql      Full PostgreSQL schema (idempotent, applied on startup)
  cli.py          Operations CLI (init-db, ingest, reclassify, import-boards)
  static/         Dashboard: index.html, app.js, styles.css (no third-party runtime assets)
data/
  india-boards.seed.json   Curated Indian-company board slugs merged on every run
n8n/
  jobhunt-india-ingestion.json               Schedule + manual ingestion workflow
  jobhunt-india-continuous-deployment.json   GitHub -> Coolify deployment bridge
  credential.template.json                   Header-auth credential templates
tests/
  test_classifier.py   Classifier behaviour
  test_assets.py        Workflow validity, schema invariants, dashboard asset hygiene
Dockerfile        Two-stage build; vendors job-boards at a pinned commit
.github/workflows/ci-cd.yml   Test + build on every PR/push; gated deploy on main
```

The upstream `job-boards` source is **not** committed. It is cloned into the production
image at Dockerfile build time from commit
`da7885cff552c513319318f2f31ed23f049f426e` and mounted at `/opt/job-boards`
(`JOB_BOARDS_PATH`). For local runs, place it next to the repo as `../job-boards-upstream`
or point `JOB_BOARDS_PATH` at a checkout.
