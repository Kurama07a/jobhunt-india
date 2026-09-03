from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Response, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app.db import close_pool, fetch_all, fetch_one, open_pool, pool
from app.ingestion import create_or_get_run, ensure_seed_boards, launch_ingestion


STATIC_DIR = Path(__file__).with_name("static")
VALID_LEVELS = {"internship", "entry", "mid", "senior", "unknown"}
VALID_ATS = {"ashby", "greenhouse", "lever", "smartrecruiters", "workable"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    open_pool()
    with pool.connection() as conn:
        conn.execute(
            """
            UPDATE ingestion_runs
            SET status = 'failed', finished_at = now(),
                error = COALESCE(error, 'application restarted during ingestion')
            WHERE status IN ('queued', 'running')
                AND requested_at < now() - interval '2 hours'
            """
        )
    ensure_seed_boards()
    yield
    close_pool()


app = FastAPI(
    title="JobHunt India",
    description="Entry-first software engineering job discovery for India",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=86400"
    return response


def _verify_admin(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, settings.ingest_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid ingestion token")


class IngestionRequest(BaseModel):
    mode: Literal["incremental", "refresh_recent", "full_discovery"] = "incremental"


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    return PlainTextResponse("User-agent: *\nAllow: /\n")


@app.get("/health")
def health(response: Response) -> dict:
    try:
        row = fetch_one(
            """
            SELECT
                (SELECT value FROM schema_meta WHERE key = 'schema_version') AS schema_version,
                (SELECT count(*) FROM job_boards WHERE is_active) AS active_boards,
                (SELECT count(*) FROM jobs WHERE is_active) AS active_jobs
            """
        )
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unhealthy", "database": "unavailable", "detail": type(exc).__name__}
    return {"status": "healthy", "database": "connected", **row}


@app.get("/api/stats")
def stats() -> dict:
    counts = fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE is_active) AS active_jobs,
            count(*) FILTER (
                WHERE is_active AND COALESCE(published_at, first_seen_at) >= now() - interval '24 hours'
            ) AS posted_24h,
            count(*) FILTER (
                WHERE is_active AND experience_level IN ('internship', 'entry')
            ) AS entry_jobs,
            count(*) FILTER (WHERE is_active AND is_remote) AS remote_jobs,
            max(last_seen_at) FILTER (WHERE is_active) AS newest_sync
        FROM jobs
        """
    )
    board_counts = fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE is_active) AS active_boards,
            count(*) FILTER (WHERE is_india_company AND is_active) AS indian_company_boards,
            count(*) FILTER (WHERE last_checked_at >= now() - interval '24 hours') AS checked_24h
        FROM job_boards
        """
    )
    latest = fetch_one(
        """
        SELECT id::text, mode, status, requested_at, started_at, finished_at,
            boards_total, boards_checked, boards_failed, boards_unchanged,
            jobs_seen, jobs_targeted, jobs_upserted
        FROM ingestion_runs ORDER BY requested_at DESC LIMIT 1
        """
    )
    return {**counts, **board_counts, "latest_run": latest}


@app.get("/api/filters")
def filters() -> dict:
    companies = fetch_all(
        """
        SELECT company AS value, count(*) AS count FROM jobs
        WHERE is_active GROUP BY company ORDER BY count(*) DESC, company LIMIT 250
        """
    )
    locations = fetch_all(
        """
        SELECT location AS value, count(*) AS count FROM jobs
        WHERE is_active AND location <> ''
        GROUP BY location ORDER BY count(*) DESC, location LIMIT 180
        """
    )
    skills = fetch_all(
        """
        SELECT skill AS value, count(*) AS count
        FROM jobs, unnest(skills) AS skill
        WHERE is_active GROUP BY skill ORDER BY count(*) DESC, skill LIMIT 80
        """
    )
    sources = fetch_all(
        """
        SELECT ats AS value, count(*) AS count FROM jobs
        WHERE is_active GROUP BY ats ORDER BY ats
        """
    )
    return {
        "companies": companies,
        "locations": locations,
        "skills": skills,
        "sources": sources,
        "levels": ["internship", "entry", "unknown", "mid", "senior"],
    }


@app.get("/api/jobs")
def list_jobs(
    q: str = Query("", max_length=120),
    levels: str = Query("", max_length=100),
    days: int | None = Query(None, ge=1, le=3650),
    remote: bool | None = None,
    ats: str = Query("", max_length=80),
    company: str = Query("", max_length=180),
    location: str = Query("", max_length=180),
    skills: str = Query("", max_length=300),
    employment_type: str = Query("", max_length=120),
    max_experience: float | None = Query(None, ge=0, le=30),
    explicit_experience: bool | None = None,
    sort: Literal["entry", "recent", "experience", "company"] = "entry",
    page: int = Query(1, ge=1, le=10000),
    page_size: int = Query(24, ge=1, le=100),
) -> dict:
    where = ["j.is_active = true"]
    params: list[object] = []

    q = q.strip()
    if q:
        where.append(
            """(
                j.search_document @@ websearch_to_tsquery('english', %s)
                OR j.title ILIKE %s OR j.company ILIKE %s
                OR EXISTS (
                    SELECT 1 FROM unnest(j.skills) AS searched_skill
                    WHERE searched_skill ILIKE %s
                )
            )"""
        )
        wildcard = f"%{q}%"
        params.extend((q, wildcard, wildcard, wildcard))

    requested_levels = [value.strip() for value in levels.split(",") if value.strip()]
    if requested_levels:
        invalid = set(requested_levels) - VALID_LEVELS
        if invalid:
            raise HTTPException(422, f"invalid experience levels: {', '.join(sorted(invalid))}")
        where.append("j.experience_level = ANY(%s)")
        params.append(requested_levels)

    if days is not None:
        where.append("COALESCE(j.published_at, j.first_seen_at) >= now() - (%s * interval '1 day')")
        params.append(days)
    if remote is not None:
        where.append("j.is_remote = %s")
        params.append(remote)

    requested_ats = [value.strip() for value in ats.split(",") if value.strip()]
    if requested_ats:
        invalid_ats = set(requested_ats) - VALID_ATS
        if invalid_ats:
            raise HTTPException(422, f"invalid sources: {', '.join(sorted(invalid_ats))}")
        where.append("j.ats = ANY(%s)")
        params.append(requested_ats)

    if company.strip():
        where.append("j.company = %s")
        params.append(company.strip())
    if location.strip():
        where.append("j.location ILIKE %s")
        params.append(f"%{location.strip()}%")

    requested_skills = [value.strip() for value in skills.split(",") if value.strip()]
    if requested_skills:
        where.append("j.skills && %s")
        params.append(requested_skills)
    if employment_type.strip():
        where.append("j.employment_type ILIKE %s")
        params.append(f"%{employment_type.strip()}%")
    if max_experience is not None:
        # A numeric ceiling is meaningful only when the publisher stated a
        # requirement; inferred/unknown roles remain available via the level chips.
        where.append("j.experience_is_explicit = true")
        where.append("COALESCE(j.experience_min, 0) <= %s")
        params.append(max_experience)
    if explicit_experience is not None:
        where.append("j.experience_is_explicit = %s")
        params.append(explicit_experience)

    order_by = {
        "entry": "j.entry_level_score DESC, COALESCE(j.published_at, j.first_seen_at) DESC, j.title",
        "recent": "COALESCE(j.published_at, j.first_seen_at) DESC, j.entry_level_score DESC",
        "experience": "j.experience_min ASC NULLS LAST, j.entry_level_score DESC, COALESCE(j.published_at, j.first_seen_at) DESC",
        "company": "lower(j.company), COALESCE(j.published_at, j.first_seen_at) DESC",
    }[sort]
    offset = (page - 1) * page_size
    query = f"""
        SELECT
            j.id::text, j.ats, j.source_job_id, j.company, j.title, j.department,
            j.team, j.employment_type, j.location, j.city, j.is_remote,
            j.workplace_type, j.published_at, j.first_seen_at, j.last_seen_at,
            j.description_excerpt, j.apply_url, j.india_match_reason,
            j.experience_min, j.experience_max, j.experience_level,
            j.experience_is_explicit, j.entry_level_score, j.skills,
            j.salary_min, j.salary_max, j.salary_currency, j.salary_period,
            greatest(0, floor(extract(epoch FROM (
                now() - COALESCE(j.published_at, j.first_seen_at)
            )) / 86400))::integer AS days_posted,
            count(*) OVER() AS total_count
        FROM jobs j
        WHERE {' AND '.join(where)}
        ORDER BY {order_by}
        LIMIT %s OFFSET %s
    """
    rows = fetch_all(query, [*params, page_size, offset])
    total = rows[0].pop("total_count") if rows else 0
    for row in rows[1:]:
        row.pop("total_count", None)
    return {
        "jobs": rows,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size if total else 0,
        },
    }


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: UUID) -> dict:
    row = fetch_one(
        """
        SELECT id::text, ats, source_job_id, company, title, department, team,
            employment_type, location, city, is_remote, workplace_type,
            published_at, first_seen_at, last_seen_at, description, apply_url,
            india_match_reason, experience_min, experience_max, experience_level,
            experience_is_explicit, entry_level_score, skills, salary_min,
            salary_max, salary_currency, salary_period
        FROM jobs WHERE id = %s AND is_active
        """,
        (job_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="job not found")
    return row


@app.get("/api/sync-status")
def sync_status() -> dict:
    latest = fetch_one(
        """
        SELECT id::text, mode, status, requested_at, started_at, finished_at,
            boards_total, boards_checked, boards_succeeded, boards_failed,
            boards_unchanged, boards_discovered, jobs_seen, jobs_targeted,
            jobs_upserted, jobs_closed
        FROM ingestion_runs ORDER BY requested_at DESC LIMIT 1
        """
    )
    return latest or {"status": "never_run"}


@app.post("/api/admin/ingest", status_code=status.HTTP_202_ACCEPTED)
def start_ingestion(
    request: IngestionRequest,
    x_ingest_token: str | None = Header(None, alias="X-Ingest-Token"),
) -> dict:
    _verify_admin(x_ingest_token)
    run_id, created = create_or_get_run(request.mode)
    if created:
        launch_ingestion(run_id, request.mode)
    return {
        "run_id": run_id,
        "status": "queued" if created else "already_running",
        "created": created,
        "mode": request.mode,
    }


@app.get("/api/admin/runs/{run_id}")
def ingestion_status(
    run_id: UUID,
    x_ingest_token: str | None = Header(None, alias="X-Ingest-Token"),
) -> dict:
    _verify_admin(x_ingest_token)
    row = fetch_one(
        """
        SELECT id::text, mode, status, requested_at, started_at, finished_at,
            boards_total, boards_checked, boards_succeeded, boards_failed,
            boards_unchanged, boards_discovered, jobs_seen, jobs_targeted,
            jobs_upserted, jobs_closed, error, metadata
        FROM ingestion_runs WHERE id = %s
        """,
        (run_id,),
    )
    if not row:
        raise HTTPException(status_code=404, detail="ingestion run not found")
    return row


@app.get("/api/admin/coverage")
def coverage(
    x_ingest_token: str | None = Header(None, alias="X-Ingest-Token"),
) -> dict:
    """The discovery -> classification funnel, for spotting where coverage leaks."""
    _verify_admin(x_ingest_token)
    boards = fetch_one(
        """
        SELECT
            count(*) AS total,
            count(*) FILTER (WHERE is_active) AS active,
            count(*) FILTER (WHERE is_india_company) AS india_company,
            count(*) FILTER (WHERE NOT is_active) AS dead,
            count(*) FILTER (WHERE consecutive_failures > 0) AS failing,
            count(*) FILTER (WHERE last_discovered_at IS NOT NULL) AS directed_confirmed
        FROM job_boards
        """
    )
    by_ats = fetch_all(
        """
        SELECT b.ats,
            count(*) FILTER (WHERE b.is_active) AS boards,
            count(*) FILTER (WHERE b.is_india_company) AS india_company_boards,
            COALESCE(j.productive_boards, 0) AS productive_boards,
            COALESCE(j.active_jobs, 0) AS active_jobs
        FROM job_boards b
        LEFT JOIN (
            SELECT ats,
                count(DISTINCT board_slug) AS productive_boards,
                count(*) AS active_jobs
            FROM jobs WHERE is_active GROUP BY ats
        ) j ON j.ats = b.ats
        GROUP BY b.ats, j.productive_boards, j.active_jobs
        ORDER BY b.ats
        """
    )
    jobs = fetch_one(
        """
        SELECT
            count(*) FILTER (WHERE is_active) AS active,
            count(*) FILTER (WHERE is_active AND experience_level IN ('internship', 'entry')) AS early_career,
            count(*) FILTER (WHERE is_active AND is_remote) AS remote,
            count(*) FILTER (WHERE is_active AND COALESCE(published_at, first_seen_at) >= now() - interval '30 days') AS fresh_30d,
            count(*) FILTER (WHERE is_active AND COALESCE(published_at, first_seen_at) < now() - interval '90 days') AS older_90d,
            percentile_cont(0.5) WITHIN GROUP (
                ORDER BY extract(epoch FROM now() - COALESCE(published_at, first_seen_at)) / 86400
            ) FILTER (WHERE is_active) AS median_age_days
        FROM jobs
        """
    )
    by_reason = fetch_all(
        """
        SELECT india_match_reason AS reason, count(*) AS count
        FROM jobs WHERE is_active
        GROUP BY india_match_reason ORDER BY count DESC
        """
    )
    by_level = fetch_all(
        """
        SELECT experience_level AS level, count(*) AS count
        FROM jobs WHERE is_active
        GROUP BY experience_level ORDER BY count DESC
        """
    )
    discovery_trend = fetch_all(
        """
        SELECT mode, status, requested_at, boards_discovered, jobs_targeted
        FROM ingestion_runs ORDER BY requested_at DESC LIMIT 12
        """
    )
    return {
        "boards": boards,
        "boards_by_ats": by_ats,
        "jobs": jobs,
        "jobs_by_india_match_reason": by_reason,
        "jobs_by_experience_level": by_level,
        "recent_runs": discovery_trend,
    }
