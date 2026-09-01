from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from app.classifier import (
    classify_job,
    content_hash,
    display_company,
    is_known_indian_company,
    is_software_role,
    location_is_india,
    plain_text,
)
from app.config import settings
from app.db import fetch_all, fetch_one, pool


ADVISORY_LOCK_ID = 4_912_024_091
ALLOWED_MODES = {"incremental", "refresh_recent", "full_discovery", "smoke"}
PROGRESS_INTERVAL = 25
MAX_DESCRIPTION_LENGTH = 60_000


def _load_upstream():
    default_path = Path(__file__).resolve().parents[2] / "job-boards-upstream"
    source_dir = Path(os.getenv("JOB_BOARDS_PATH", default_path)).resolve()
    source_file = source_dir / "job_boards.py"
    if not source_file.exists():
        raise RuntimeError(f"job-boards upstream source not found at {source_file}")
    spec = importlib.util.spec_from_file_location("job_boards_upstream", source_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {source_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


upstream = _load_upstream()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _description_excerpt(description: str, length: int = 420) -> str:
    if len(description) <= length:
        return description
    clipped = description[:length].rsplit(" ", 1)[0]
    return f"{clipped}…"


def import_boards_payload(payload: dict[str, list[str]], discovered_via: str) -> int:
    rows: list[dict[str, Any]] = []
    for ats in upstream.SOURCES:
        for slug in payload.get(ats, []):
            if not isinstance(slug, str) or not upstream.plausible(slug, ats):
                continue
            rows.append(
                {
                    "ats": ats,
                    "slug": slug,
                    "display_name": display_company(slug),
                    "is_india_company": is_known_indian_company(slug),
                    "discovered_via": discovered_via,
                }
            )
    if not rows:
        return 0
    query = """
        INSERT INTO job_boards (
            ats, slug, display_name, is_india_company, discovered_via, is_active
        ) VALUES (
            %(ats)s, %(slug)s, %(display_name)s, %(is_india_company)s,
            %(discovered_via)s, true
        )
        ON CONFLICT (ats, slug) DO UPDATE SET
            display_name = excluded.display_name,
            is_india_company = job_boards.is_india_company OR excluded.is_india_company,
            is_active = true,
            updated_at = now()
    """
    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.executemany(query, rows)
    return len(rows)


def import_boards_file(path: str | Path, discovered_via: str = "import") -> int:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("boards file must be an object keyed by ATS")
    return import_boards_payload(payload, discovered_via)


def ensure_seed_boards() -> int:
    seed_path = Path(upstream.__file__).with_name("boards.seed.json")
    imported = import_boards_file(seed_path, "upstream_seed")
    india_seed = Path(__file__).resolve().parents[1] / "data" / "india-boards.seed.json"
    if india_seed.exists():
        imported += import_boards_file(india_seed, "india_seed")
    return imported


def _write_upstream_cache_from_db() -> None:
    rows = fetch_all(
        "SELECT ats, slug FROM job_boards WHERE is_active ORDER BY ats, lower(slug)"
    )
    payload = {ats: [] for ats in upstream.SOURCES}
    for row in rows:
        payload[row["ats"]].append(row["slug"])
    cache_path = Path(upstream.__file__).with_name("boards.json")
    cache_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def refresh_boards(mode: str) -> int:
    _write_upstream_cache_from_db()
    recent = mode == "refresh_recent"
    payload = upstream.load_boards(
        refresh=True,
        ats_list=list(upstream.SOURCES),
        concurrency=settings.max_workers,
        recent=recent,
    )
    before = fetch_one("SELECT count(*) AS count FROM job_boards")["count"]
    import_boards_payload(payload, "recent_discovery" if recent else "full_discovery")
    after = fetch_one("SELECT count(*) AS count FROM job_boards")["count"]
    return max(0, after - before)


def _fetch_greenhouse_description(slug: str, source_job_id: str) -> str:
    url = (
        "https://boards-api.greenhouse.io/v1/boards/"
        f"{urllib.parse.quote(slug)}/jobs/{urllib.parse.quote(source_job_id)}?content=true"
    )
    try:
        payload = json.loads(upstream.fetch(url))
    except Exception:
        return ""
    return payload.get("content") or "" if isinstance(payload, dict) else ""


def _fetch_board(board: dict[str, Any], use_etag: bool) -> dict[str, Any]:
    ats = board["ats"]
    slug = board["slug"]
    meta: dict[str, Any] = {}
    try:
        raw = upstream.fetch(
            upstream.board_url(ats, slug),
            etag=board.get("etag") if use_etag else None,
            meta=meta,
        )
    except upstream.NotModified:
        return {"status": "unchanged", "ats": ats, "slug": slug}
    except upstream.NotFound:
        return {"status": "dead", "ats": ats, "slug": slug, "error": "board returned 404"}
    except Exception as exc:
        return {
            "status": "error",
            "ats": ats,
            "slug": slug,
            "error": f"{type(exc).__name__}: {exc}"[:900],
        }

    try:
        payload = json.loads(raw)
        jobs = upstream.SOURCES[ats]["jobs"](payload)
        if not isinstance(jobs, list):
            raise ValueError("response has no jobs array")
    except Exception as exc:
        return {
            "status": "error",
            "ats": ats,
            "slug": slug,
            "error": f"invalid payload: {type(exc).__name__}: {exc}"[:900],
        }

    normalized: list[dict[str, Any]] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        norm = upstream.SOURCES[ats]["normalize"](job)
        if norm is None or not norm.get("id"):
            continue
        normalized.append(upstream._clean(norm))

    effective_india_company = bool(board.get("is_india_company")) or is_known_indian_company(slug)
    target_rows: list[dict[str, Any]] = []

    for norm in normalized:
        title = norm.get("title", "")
        department = norm.get("department", "")
        team = norm.get("team", "")
        description = plain_text(norm.get("_description", ""))

        prelim_software = is_software_role(title, department, team, description)
        prelim_india = (
            location_is_india(norm.get("location", ""))
            or effective_india_company
            or is_known_indian_company(slug)
        )
        if ats == "greenhouse" and prelim_software and prelim_india:
            detail = _fetch_greenhouse_description(slug, str(norm["id"]))
            if detail:
                description = plain_text(detail)

        classification = classify_job(
            title=title,
            description=description,
            location=norm.get("location", ""),
            board_slug=slug,
            board_is_india=effective_india_company,
            is_remote=bool(norm.get("isRemote")),
            department=department,
            team=team,
        )
        if not classification.is_target:
            continue

        description = description[:MAX_DESCRIPTION_LENGTH]
        metadata = {
            "department": department,
            "team": team,
            "workplace_type": norm.get("workplaceType", ""),
            "source_published_at": norm.get("publishedAt", ""),
        }
        row = {
            "ats": ats,
            "source_job_id": str(norm["id"]),
            "board_slug": slug,
            "company": board.get("display_name") or display_company(slug),
            "title": title,
            "department": department,
            "team": team,
            "employment_type": norm.get("employmentType", ""),
            "location": norm.get("location", ""),
            "city": classification.city,
            "is_remote": bool(norm.get("isRemote")),
            "workplace_type": norm.get("workplaceType", ""),
            "published_at": _parse_timestamp(norm.get("publishedAt")),
            "description": description,
            "description_excerpt": _description_excerpt(description),
            "apply_url": norm.get("jobUrl", ""),
            "india_match_reason": classification.india_match_reason,
            "experience_min": classification.experience_min,
            "experience_max": classification.experience_max,
            "experience_level": classification.experience_level,
            "experience_is_explicit": classification.experience_is_explicit,
            "entry_level_score": classification.entry_level_score,
            "skills": classification.skills,
            "salary_min": classification.salary_min,
            "salary_max": classification.salary_max,
            "salary_currency": classification.salary_currency,
            "salary_period": classification.salary_period,
            "raw_metadata": Jsonb(metadata),
        }
        row["content_hash"] = content_hash({k: v for k, v in row.items() if k != "raw_metadata"})
        target_rows.append(row)

    return {
        "status": "modified",
        "ats": ats,
        "slug": slug,
        "etag": meta.get("etag"),
        "jobs_seen": len(normalized),
        "target_rows": target_rows,
        "is_india_company": effective_india_company,
    }


UPSERT_JOB_SQL = """
    INSERT INTO jobs (
        ats, source_job_id, board_slug, company, title, department, team,
        employment_type, location, city, is_remote, workplace_type, published_at,
        description, description_excerpt, apply_url, india_match_reason,
        experience_min, experience_max, experience_level, experience_is_explicit,
        entry_level_score, skills, salary_min, salary_max, salary_currency,
        salary_period, content_hash, raw_metadata
    ) VALUES (
        %(ats)s, %(source_job_id)s, %(board_slug)s, %(company)s, %(title)s,
        %(department)s, %(team)s, %(employment_type)s, %(location)s, %(city)s,
        %(is_remote)s, %(workplace_type)s, %(published_at)s, %(description)s,
        %(description_excerpt)s, %(apply_url)s, %(india_match_reason)s,
        %(experience_min)s, %(experience_max)s, %(experience_level)s,
        %(experience_is_explicit)s, %(entry_level_score)s, %(skills)s,
        %(salary_min)s, %(salary_max)s, %(salary_currency)s, %(salary_period)s,
        %(content_hash)s, %(raw_metadata)s
    )
    ON CONFLICT (ats, source_job_id) DO UPDATE SET
        board_slug = excluded.board_slug,
        company = excluded.company,
        title = excluded.title,
        department = excluded.department,
        team = excluded.team,
        employment_type = excluded.employment_type,
        location = excluded.location,
        city = excluded.city,
        is_remote = excluded.is_remote,
        workplace_type = excluded.workplace_type,
        published_at = COALESCE(excluded.published_at, jobs.published_at),
        last_seen_at = now(),
        closed_at = NULL,
        is_active = true,
        description = CASE
            WHEN excluded.description <> '' THEN excluded.description ELSE jobs.description
        END,
        description_excerpt = CASE
            WHEN excluded.description_excerpt <> '' THEN excluded.description_excerpt
            ELSE jobs.description_excerpt
        END,
        apply_url = excluded.apply_url,
        india_match_reason = excluded.india_match_reason,
        experience_min = excluded.experience_min,
        experience_max = excluded.experience_max,
        experience_level = excluded.experience_level,
        experience_is_explicit = excluded.experience_is_explicit,
        entry_level_score = excluded.entry_level_score,
        skills = excluded.skills,
        salary_min = excluded.salary_min,
        salary_max = excluded.salary_max,
        salary_currency = excluded.salary_currency,
        salary_period = excluded.salary_period,
        content_hash = excluded.content_hash,
        raw_metadata = excluded.raw_metadata,
        updated_at = now()
"""


def reclassify_existing_jobs() -> dict[str, int]:
    """Re-evaluate stored jobs after classifier changes without refetching ATS data."""
    rows = fetch_all(
        """
        SELECT j.id::text, j.title, j.description, j.location, j.board_slug,
            j.is_remote, j.department, j.team,
            COALESCE(b.is_india_company, false) AS board_is_india
        FROM jobs j
        LEFT JOIN job_boards b
            ON b.ats = j.ats AND b.slug = j.board_slug
        WHERE j.is_active
        """
    )
    updates: list[dict[str, Any]] = []
    closures: list[tuple[str]] = []
    for row in rows:
        classification = classify_job(
            title=row["title"],
            description=row["description"],
            location=row["location"],
            board_slug=row["board_slug"],
            board_is_india=bool(row["board_is_india"]),
            is_remote=bool(row["is_remote"]),
            department=row["department"],
            team=row["team"],
        )
        if not classification.is_target:
            closures.append((row["id"],))
            continue
        updates.append(
            {
                "id": row["id"],
                "city": classification.city,
                "india_match_reason": classification.india_match_reason,
                "experience_min": classification.experience_min,
                "experience_max": classification.experience_max,
                "experience_level": classification.experience_level,
                "experience_is_explicit": classification.experience_is_explicit,
                "entry_level_score": classification.entry_level_score,
                "skills": classification.skills,
                "salary_min": classification.salary_min,
                "salary_max": classification.salary_max,
                "salary_currency": classification.salary_currency,
                "salary_period": classification.salary_period,
            }
        )

    with pool.connection() as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                if updates:
                    cursor.executemany(
                        """
                        UPDATE jobs SET
                            city = %(city)s,
                            india_match_reason = %(india_match_reason)s,
                            experience_min = %(experience_min)s,
                            experience_max = %(experience_max)s,
                            experience_level = %(experience_level)s,
                            experience_is_explicit = %(experience_is_explicit)s,
                            entry_level_score = %(entry_level_score)s,
                            skills = %(skills)s,
                            salary_min = %(salary_min)s,
                            salary_max = %(salary_max)s,
                            salary_currency = %(salary_currency)s,
                            salary_period = %(salary_period)s,
                            updated_at = now()
                        WHERE id = %(id)s::uuid
                        """,
                        updates,
                    )
                if closures:
                    cursor.executemany(
                        """
                        UPDATE jobs SET is_active = false,
                            closed_at = COALESCE(closed_at, now()), updated_at = now()
                        WHERE id = %s::uuid AND is_active
                        """,
                        closures,
                    )
    return {"scanned": len(rows), "updated": len(updates), "closed": len(closures)}


def _persist_board_result(result: dict[str, Any]) -> dict[str, int]:
    status = result["status"]
    ats = result["ats"]
    slug = result["slug"]
    counts = {"succeeded": 0, "failed": 0, "unchanged": 0, "seen": 0, "targeted": 0, "upserted": 0, "closed": 0}

    with pool.connection() as conn:
        with conn.transaction():
            if status == "unchanged":
                conn.execute(
                    """
                    UPDATE job_boards SET last_checked_at = now(), last_success_at = now(),
                        consecutive_failures = 0, last_error = NULL, updated_at = now()
                    WHERE ats = %s AND slug = %s
                    """,
                    (ats, slug),
                )
                counts["succeeded"] = 1
                counts["unchanged"] = 1
                return counts

            if status == "dead":
                conn.execute(
                    """
                    UPDATE job_boards SET is_active = false, last_checked_at = now(),
                        last_error = %s, consecutive_failures = consecutive_failures + 1,
                        updated_at = now() WHERE ats = %s AND slug = %s
                    """,
                    (result.get("error"), ats, slug),
                )
                closed = conn.execute(
                    """
                    UPDATE jobs SET is_active = false, closed_at = COALESCE(closed_at, now()),
                        updated_at = now()
                    WHERE ats = %s AND board_slug = %s AND is_active
                    RETURNING 1
                    """,
                    (ats, slug),
                ).fetchall()
                counts["failed"] = 1
                counts["closed"] = len(closed)
                return counts

            if status == "error":
                conn.execute(
                    """
                    UPDATE job_boards SET last_checked_at = now(), last_error = %s,
                        consecutive_failures = consecutive_failures + 1, updated_at = now()
                    WHERE ats = %s AND slug = %s
                    """,
                    (result.get("error"), ats, slug),
                )
                counts["failed"] = 1
                return counts

            rows = result["target_rows"]
            source_ids = [row["source_job_id"] for row in rows]
            if rows:
                with conn.cursor() as cursor:
                    cursor.executemany(UPSERT_JOB_SQL, rows)
            closed = conn.execute(
                """
                UPDATE jobs SET is_active = false, closed_at = COALESCE(closed_at, now()),
                    updated_at = now()
                WHERE ats = %s AND board_slug = %s AND is_active
                    AND NOT (source_job_id = ANY(%s))
                RETURNING 1
                """,
                (ats, slug, source_ids),
            ).fetchall()
            conn.execute(
                """
                UPDATE job_boards SET
                    is_active = true,
                    is_india_company = is_india_company OR %s,
                    etag = COALESCE(%s, etag),
                    last_checked_at = now(),
                    last_success_at = now(),
                    last_error = NULL,
                    consecutive_failures = 0,
                    jobs_seen = %s,
                    updated_at = now()
                WHERE ats = %s AND slug = %s
                """,
                (
                    result.get("is_india_company", False),
                    result.get("etag"),
                    result["jobs_seen"],
                    ats,
                    slug,
                ),
            )
            counts.update(
                {
                    "succeeded": 1,
                    "seen": result["jobs_seen"],
                    "targeted": len(rows),
                    "upserted": len(rows),
                    "closed": len(closed),
                }
            )
            return counts


def create_or_get_run(mode: str) -> tuple[str, bool]:
    if mode not in ALLOWED_MODES:
        raise ValueError(f"unsupported ingestion mode: {mode}")
    with pool.connection() as conn:
        with conn.transaction():
            existing = conn.execute(
                """
                SELECT id::text FROM ingestion_runs
                WHERE status IN ('queued', 'running')
                ORDER BY requested_at DESC LIMIT 1 FOR UPDATE
                """
            ).fetchone()
            if existing:
                return existing["id"], False
            row = conn.execute(
                """
                INSERT INTO ingestion_runs (mode, status)
                VALUES (%s, 'queued') RETURNING id::text
                """,
                (mode,),
            ).fetchone()
            return row["id"], True


def _update_run(run_id: str, counters: dict[str, int], **extra: Any) -> None:
    assignments = [
        "boards_checked = %s",
        "boards_succeeded = %s",
        "boards_failed = %s",
        "boards_unchanged = %s",
        "jobs_seen = %s",
        "jobs_targeted = %s",
        "jobs_upserted = %s",
        "jobs_closed = %s",
    ]
    values: list[Any] = [
        counters["checked"],
        counters["succeeded"],
        counters["failed"],
        counters["unchanged"],
        counters["seen"],
        counters["targeted"],
        counters["upserted"],
        counters["closed"],
    ]
    for key, value in extra.items():
        assignments.append(f"{key} = %s")
        values.append(value)
    values.append(run_id)
    with pool.connection() as conn:
        conn.execute(
            f"UPDATE ingestion_runs SET {', '.join(assignments)} WHERE id = %s",
            values,
        )


def run_ingestion(run_id: str, mode: str, limit_per_ats: int | None = None) -> None:
    counters = {
        "checked": 0,
        "succeeded": 0,
        "failed": 0,
        "unchanged": 0,
        "seen": 0,
        "targeted": 0,
        "upserted": 0,
        "closed": 0,
    }
    with pool.connection() as lock_conn:
        locked = lock_conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS locked", (ADVISORY_LOCK_ID,)
        ).fetchone()["locked"]
        if not locked:
            _update_run(
                run_id,
                counters,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                error="another ingestion holds the database lock",
            )
            return
        try:
            _update_run(
                run_id,
                counters,
                status="running",
                started_at=datetime.now(timezone.utc),
            )
            ensure_seed_boards()
            discovered = 0
            if mode in {"refresh_recent", "full_discovery"}:
                discovered = refresh_boards(mode)

            boards = fetch_all(
                """
                SELECT ats, slug, display_name, is_india_company, etag
                FROM job_boards WHERE is_active
                ORDER BY ats, lower(slug)
                """
            )
            effective_limit = limit_per_ats
            if mode == "smoke" and effective_limit is None:
                effective_limit = 2
            if effective_limit:
                kept: list[dict[str, Any]] = []
                platform_counts: dict[str, int] = {}
                for board in boards:
                    count = platform_counts.get(board["ats"], 0)
                    if count < effective_limit:
                        kept.append(board)
                        platform_counts[board["ats"]] = count + 1
                boards = kept

            _update_run(
                run_id,
                counters,
                boards_total=len(boards),
                boards_discovered=discovered,
            )
            use_etag = mode not in {"full_discovery", "smoke"}
            with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
                future_map = {
                    executor.submit(_fetch_board, board, use_etag): board for board in boards
                }
                for future in as_completed(future_map):
                    # Drop completed futures immediately so their parsed ATS payloads and
                    # job descriptions do not accumulate across a 13k-board sweep.
                    board = future_map.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "status": "error",
                            "ats": board["ats"],
                            "slug": board["slug"],
                            "error": f"worker failure: {type(exc).__name__}: {exc}"[:900],
                        }
                    delta = _persist_board_result(result)
                    counters["checked"] += 1
                    for key in ("succeeded", "failed", "unchanged", "seen", "targeted", "upserted", "closed"):
                        counters[key] += delta[key]
                    if counters["checked"] % PROGRESS_INTERVAL == 0:
                        _update_run(run_id, counters)

            _update_run(
                run_id,
                counters,
                status="completed",
                finished_at=datetime.now(timezone.utc),
                error=None,
            )
        except Exception as exc:
            _update_run(
                run_id,
                counters,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
        finally:
            lock_conn.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_ID,))


def launch_ingestion(run_id: str, mode: str, limit_per_ats: int | None = None) -> threading.Thread:
    thread = threading.Thread(
        target=run_ingestion,
        args=(run_id, mode, limit_per_ats),
        name=f"jobhunt-ingestion-{run_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread
