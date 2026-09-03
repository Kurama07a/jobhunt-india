# Build, CI/CD & Deployment

## Container image (`Dockerfile`)

Two stages, both pinned by digest:

### Stage `upstream` — `alpine/git`
Clones `https://github.com/mherzog4/job-boards.git`, checks out
`ARG JOB_BOARDS_COMMIT=da7885cff552c513319318f2f31ed23f049f426e`, deletes `.git`. This
vendors the discovery + ATS adapter code at an exact commit — the supply chain is frozen.

### Stage `python:3.12-slim-bookworm`
- Env: `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, `PYTHONPATH=/app`,
  `JOB_BOARDS_PATH=/opt/job-boards`, `PORT=8000`.
- Installs `curl` (used by Coolify's health checks).
- Creates non‑root user `jobhunt` (uid/gid 10001) and runs as it.
- `pip install -r requirements.txt` → `fastapi`, `psycopg[binary]`, `psycopg-pool`,
  `uvicorn[standard]` (all version‑pinned).
- Copies `job-boards` → `/opt/job-boards`, then `app/`, `data/`, `n8n/`, `README.md`,
  `LICENSE`. The upstream's own `LICENSE` remains at `/opt/job-boards/LICENSE`.
- `HEALTHCHECK` every 30s: `python -c "... urlopen('http://127.0.0.1:8000/health')"`.
- `CMD`: `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1
  --proxy-headers --forwarded-allow-ips='*'`.

**One worker is intentional** — the ingestion background thread and single‑flight run
logic assume a single process. Scale by running the sweep less often, not by adding
workers.

`.dockerignore` keeps `tests/`, `.git`, `.github`, `runtime/`, caches out of the image.

---

## Environment variables

| Var | Required | Default | Notes |
|---|---|---|---|
| `DATABASE_URL` | **yes** | — | `postgresql://user:pass@host:5432/db`. Process refuses to start without it. |
| `INGEST_TOKEN` | **yes** | — | Shared secret for `/api/admin/*`. Must match the n8n `JobHunt Ingestion API` credential. |
| `JOB_SCRAPER_CONTACT` | yes (for scraping) | — | Email placed in the outbound `User-Agent`. Required by upstream `fetch()`. |
| `ALLOWED_HOSTS` | no | `jobhunt.prakhar.wtf,localhost,127.0.0.1,testserver` | CSV; `TrustedHostMiddleware`. |
| `SCRAPER_CONCURRENCY` | no | `8` | Clamped to `1..8` in code. |
| `APP_ENV` | no | `production` | Informational. |
| `PORT` | no | `8000` | |
| `JOB_BOARDS_PATH` | no | `/opt/job-boards` (image) / `../job-boards-upstream` (repo) | Location of the vendored module. |

`.env` is gitignored. In production these are set on the Coolify service.

---

## CI/CD pipeline (`.github/workflows/ci-cd.yml`)

Triggers: every `pull_request` to `main`, every `push` to `main`, and manual
`workflow_dispatch`. `permissions: contents: read`. PR runs are cancel‑in‑progress;
`main` runs are not.

```mermaid
flowchart TD
    subgraph verify["job: verify  (every PR + push)"]
        V1["checkout"] --> V2["setup Python 3.12 + Node 20"]
        V2 --> V3["pip install -r requirements-dev.txt"]
        V3 --> V4["pytest -q"]
        V4 --> V5["python -m compileall -q app"]
        V5 --> V6["node --check app/static/app.js"]
        V6 --> V7["docker build -t jobhunt-india:$SHA ."]
    end

    verify -->|push to main only| deploy

    subgraph deploy["job: deploy  (push to main, needs verify)"]
        D1["Queue deployment in Coolify\nPOST n8n /webhook/jobhunt-cd-deploy\nX-JobHunt-Deploy-Token: CD_DEPLOY_TOKEN\nbody {repository, ref, sha, run_id}\n-> deployment_uuid"]
        D1 --> D2["Wait for exact commit\nloop up to 90x / 15 min:\nPOST n8n /webhook/jobhunt-cd-status\n{repository, deployment_uuid}"]
        D2 --> D3{"status?"}
        D3 -->|finished + commit == SHA| D4["Smoke test production"]
        D3 -->|finished + commit != SHA| DXF["fail: wrong commit deployed"]
        D3 -->|failed / cancelled| DXF2["fail"]
        D3 -->|queued / in_progress| D2
        D4 --> D5["GET /health -> status healthy & database connected\nGET / -> 200"]
    end
```

### `verify` job (gate)
1. `pytest -q` — `tests/test_classifier.py`, `tests/test_assets.py`.
2. `python -m compileall -q app` — every module imports/compiles.
3. `node --check app/static/app.js` — dashboard JS syntax.
4. `docker build` — the production image builds (includes the pinned upstream clone).

A PR cannot merge unless all four pass. No deploy runs for PRs.

### `deploy` job (main only)
Guarded by `if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'`,
`needs: verify`, `concurrency: jobhunt-india-production` (`cancel-in-progress: false` — a
deploy is never interrupted), `environment: production`.

Env: `DEPLOY_WEBHOOK_URL=https://n8n.prakhar.wtf/webhook/jobhunt-cd-deploy`,
`STATUS_WEBHOOK_URL=…/jobhunt-cd-status`, `DEPLOY_TOKEN=${{ secrets.CD_DEPLOY_TOKEN }}`,
`EXPECTED_SHA=${{ github.sha }}`.

1. **Queue deployment in Coolify** — `curl POST` to the n8n deploy webhook with
   `X-JobHunt-Deploy-Token` and a JSON body `{repository, ref, sha, run_id}`. Parses
   `.deployments[0].deployment_uuid` from the response.
2. **Wait for the exact commit to finish** — up to 90 iterations, 10s apart (~15 min).
   Each iteration `curl POST`s the status webhook with `{repository, deployment_uuid}`
   and switches on `.status`:
   - `finished` → assert `.commit == EXPECTED_SHA`; mismatch **fails the job** (guards
     against a stale build being promoted).
   - `failed` / `cancelled-by-user` → fail.
   - `queued` / `in_progress` → sleep 10s, retry.
   - anything else → fail.
   - Loop exhausted → fail ("did not finish within 15 minutes").
3. **Smoke test production** — `curl` with retries:
   - `GET https://jobhunt.prakhar.wtf/health` → `jq` assert `.status == "healthy" and
     .database == "connected"`.
   - `GET https://jobhunt.prakhar.wtf/` → must return success.

### Secrets

| Where | Name | Purpose |
|---|---|---|
| GitHub repo secret | `CD_DEPLOY_TOKEN` | auth to the n8n CD webhooks |
| n8n credential | `JobHunt CD Webhook` | same token, validates incoming GitHub calls |
| n8n credential | `JobHunt Coolify API` | Bearer token to Coolify's API |
| Coolify service env | `DATABASE_URL`, `INGEST_TOKEN`, `JOB_SCRAPER_CONTACT`, … | app runtime |

**Coolify's own git auto‑deploy is disabled** — the only path to production is a green
`verify` on `main` calling the n8n bridge. Failed CI can't reach production.

---

## Deployment topology

```mermaid
flowchart LR
    GH["GitHub Actions\n(deploy job)"] -->|"HTTPS + X-JobHunt-Deploy-Token"| N8N["n8n\nn8n.prakhar.wtf\nCD bridge workflow"]
    N8N -->|"Bearer token"| COOL["Coolify API\n10.0.3.1:8000"]
    COOL -->|"builds image, runs container"| SVC["jobhunt-india container\nuvicorn :8000"]
    SVC --> PG[("PostgreSQL")]
    GH -->|"smoke test"| SVC
    N8N2["n8n\ningestion workflow"] -->|"X-Ingest-Token"| SVC
```

To roll back: redeploy a previous commit on `main` (revert + push), or trigger the prior
image via Coolify directly. There is no blue/green — the smoke test is the safety net.
