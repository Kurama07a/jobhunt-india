"""Extra ATS platforms and directed India-focused board discovery.

The vendored ``job-boards`` project covers Ashby, Greenhouse and Lever plus
archive-based discovery for those three. This module adds:

* a native adapter for **SmartRecruiters**, whose public postings API needs
  pagination and a country filter that the upstream single-GET model does not
  express; and
* **directed discovery** — probing ATS endpoints with slug candidates generated
  from ``data/indian-companies.json`` instead of waiting for a web archive to
  capture the board.

Everything here reuses ``upstream.fetch`` (identifying User-Agent, retry/backoff,
gzip, 304/404 handling) so the good-citizen traffic contract is unchanged.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

# Imported lazily by ingestion after it has loaded the upstream module; see
# ``bind_upstream``. Keeping a module-level handle avoids re-importing per call.
_upstream: Any = None

SMARTRECRUITERS_API = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
SMARTRECRUITERS_POSTING = "https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}"
SMARTRECRUITERS_PAGE = 100
SMARTRECRUITERS_MAX_POSTINGS = 1500  # bound pagination cost per board

_COMPANY_FILE = Path(__file__).resolve().parents[1] / "data" / "indian-companies.json"
_SLUG_NOISE = re.compile(
    r"\b(?:technolog(?:y|ies)|labs?|india|private|limited|pvt|ltd|inc|corp|co|"
    r"solutions|software|systems|global|digital|payments?|cloud|money|academy|"
    r"fintech|group|holdings|ventures|the|and)\b"
)


def bind_upstream(module: Any) -> None:
    """Called once by ingestion with the loaded ``job_boards`` module."""
    global _upstream
    _upstream = module


# --------------------------------------------------------------------------- #
# SmartRecruiters adapter
# --------------------------------------------------------------------------- #


def _sr_workplace(loc: dict[str, Any]) -> tuple[bool, str]:
    if loc.get("remote"):
        return True, "Remote"
    if loc.get("hybrid"):
        return False, "Hybrid"
    return False, ""


def _sr_full_location(loc: dict[str, Any]) -> str:
    if loc.get("fullLocation"):
        return str(loc["fullLocation"])
    parts = [loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()]
    return ", ".join(p for p in parts if p)


def _sr_normalize(job: dict[str, Any], slug: str) -> dict[str, Any] | None:
    jid = str(job.get("id") or "")
    if not jid:
        return None
    loc = job.get("location") or {}
    remote, workplace = _sr_workplace(loc)
    company = job.get("company") or {}
    identifier = company.get("identifier") or slug
    department = (job.get("department") or {}).get("label") or (
        job.get("function") or {}
    ).get("label") or ""
    return {
        "id": jid,
        "title": job.get("name") or "",
        "department": department,
        "team": "",
        "employmentType": (job.get("typeOfEmployment") or {}).get("label") or "",
        "location": _sr_full_location(loc),
        "isRemote": remote,
        "workplaceType": workplace,
        "publishedAt": job.get("releasedDate") or job.get("createdOn") or "",
        "jobUrl": f"https://jobs.smartrecruiters.com/{urllib.parse.quote(identifier)}/{jid}",
        "_description": "",  # filled on demand for prelim matches, see describe()
        "_company_name": company.get("name") or "",
    }


def _sr_describe(slug: str, job_id: str) -> str:
    url = SMARTRECRUITERS_POSTING.format(
        slug=urllib.parse.quote(slug), jid=urllib.parse.quote(job_id)
    )
    try:
        payload = json.loads(_upstream.fetch(url, timeout=25, retries=2))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    sections = ((payload.get("jobAd") or {}).get("sections")) or {}
    chunks = []
    for key in ("jobDescription", "qualifications", "additionalInformation"):
        text = (sections.get(key) or {}).get("text") or ""
        if text:
            chunks.append(text)
    return " ".join(chunks)


def _sr_fetch_records(board: dict[str, Any], use_etag: bool) -> dict[str, Any]:
    """Return normalized SmartRecruiters postings for a board (India only).

    Shape matches what ingestion._fetch_board expects from a normalize pass:
      {"status": "unchanged"} |
      {"status": "dead"|"error", "error": str} |
      {"status": "ok", "normalized": [...], "etag": str|None}
    """
    slug = board["slug"]
    base = SMARTRECRUITERS_API.format(slug=urllib.parse.quote(slug))
    meta: dict[str, Any] = {}
    first = f"{base}?country=in&limit={SMARTRECRUITERS_PAGE}&offset=0"
    try:
        raw = _upstream.fetch(
            first, etag=board.get("etag") if use_etag else None, meta=meta
        )
    except _upstream.NotModified:
        return {"status": "unchanged"}
    except _upstream.NotFound:
        return {"status": "dead", "error": "board returned 404"}
    except Exception as exc:  # noqa: BLE001 - recorded, not raised
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:900]}

    try:
        page = json.loads(raw)
        total = int(page.get("totalFound") or 0)
        content = list(page.get("content") or [])
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"invalid payload: {type(exc).__name__}: {exc}"[:900]}

    # SmartRecruiters answers 200 with an empty page for an unknown company slug.
    # Treat that as an exhaustive empty response: keep the board, close its jobs.
    offset = SMARTRECRUITERS_PAGE
    while offset < min(total, SMARTRECRUITERS_MAX_POSTINGS):
        try:
            more = json.loads(
                _upstream.fetch(f"{base}?country=in&limit={SMARTRECRUITERS_PAGE}&offset={offset}")
            )
            content.extend(more.get("content") or [])
        except Exception:
            break
        offset += SMARTRECRUITERS_PAGE

    normalized: list[dict[str, Any]] = []
    for job in content:
        if isinstance(job, dict):
            norm = _sr_normalize(job, slug)
            if norm:
                normalized.append(norm)
    return {"status": "ok", "normalized": normalized, "etag": meta.get("etag")}


def _sr_probe(slug: str) -> bool:
    """True when a SmartRecruiters company slug has at least one India posting."""
    url = SMARTRECRUITERS_API.format(slug=urllib.parse.quote(slug)) + "?country=in&limit=1"
    try:
        payload = json.loads(_upstream.fetch(url, timeout=20, retries=2))
    except Exception:
        return False
    return isinstance(payload, dict) and int(payload.get("totalFound") or 0) > 0


# --------------------------------------------------------------------------- #
# Workable — public jobs marketplace, India-filtered feed
#
# Workable is an embedded widget, not a hosted careers domain, so its per-company
# boards are invisible to web archives. But jobs.workable.com runs a public jobs
# marketplace with a location filter and full descriptions inline. We pull that
# whole India feed once per sweep, group by company, and serve each company from
# the cache — so a Workable "board" fits the same fetch_records contract as the
# others without any per-board HTTP.
# --------------------------------------------------------------------------- #

WORKABLE_FEED = "https://jobs.workable.com/api/v1/jobs?location=india"
WORKABLE_MAX_PAGES = 400
WORKABLE_PAGE_DELAY = 0.4  # be a good citizen; jobs.workable.com rate-limits bursts
_WORKABLE_WORKPLACE = {"on_site": "On-site", "remote": "Remote", "hybrid": "Hybrid"}

# company slug -> list of raw marketplace job dicts; None until the feed is pulled.
_workable_cache: dict[str, list[dict[str, Any]]] | None = None
_workable_titles: dict[str, str] = {}
# True only after a pull that paginated all the way to the last page. While False,
# a Workable board fetch reports "unchanged" so a truncated/failed feed can never
# close a company's jobs.
_workable_feed_ok: bool = False


def _wk_slug(job: dict[str, Any]) -> str:
    company = job.get("company") or {}
    url = company.get("url") or ""
    match = re.search(r"jobs-at-(.+)$", url)
    raw = urllib.parse.unquote(match.group(1)) if match else (company.get("id") or "")
    return re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-")[:60]


def _wk_normalize(job: dict[str, Any]) -> dict[str, Any] | None:
    jid = str(job.get("id") or "")
    if not jid:
        return None
    loc = job.get("location") or {}
    locs = [x for x in (job.get("locations") or []) if isinstance(x, str)]
    location = locs[0] if locs else ", ".join(
        p for p in (loc.get("city"), loc.get("subregion"), loc.get("countryName")) if p
    )
    workplace = job.get("workplace") or ""
    company = job.get("company") or {}
    return {
        "id": jid,
        "title": job.get("title") or "",
        "department": job.get("department") or "",
        "team": "",
        "employmentType": job.get("employmentType") or "",
        "location": location,
        "isRemote": workplace == "remote",
        "workplaceType": _WORKABLE_WORKPLACE.get(workplace, ""),
        "publishedAt": job.get("created") or job.get("updated") or "",
        "jobUrl": job.get("url") or "",
        "_description": job.get("description") or "",
        "_company_name": company.get("title") or "",
    }


def load_workable_feed(force: bool = False) -> dict[str, str]:
    """Pull the whole India Workable marketplace feed and cache it by company slug.

    Idempotent within a process unless ``force``. Returns slug -> company display
    title. A new cache is committed **only** if pagination reached the last page —
    a truncated pull (rate limit, network) leaves the previous cache untouched and
    ``_workable_feed_ok`` False, so no Workable jobs get closed on bad data.
    """
    global _workable_cache, _workable_feed_ok
    if _workable_cache is not None and not force:
        return dict(_workable_titles)

    cache: dict[str, list[dict[str, Any]]] = {}
    titles: dict[str, str] = {}
    token: str | None = None
    completed = False
    for _ in range(WORKABLE_MAX_PAGES):
        url = WORKABLE_FEED + (f"&pageToken={urllib.parse.quote(token)}" if token else "")
        try:
            payload = json.loads(_upstream.fetch(url, timeout=25, retries=5))
        except Exception:
            break  # incomplete — fall through without committing
        if not isinstance(payload, dict):
            break
        for job in payload.get("jobs") or []:
            if not isinstance(job, dict):
                continue
            slug = _wk_slug(job)
            if not slug or not _upstream.plausible(slug, "workable"):
                continue
            cache.setdefault(slug, []).append(job)
            titles[slug] = (job.get("company") or {}).get("title") or slug
        token = payload.get("nextPageToken")
        if not token:
            completed = True
            break
        time.sleep(WORKABLE_PAGE_DELAY)

    if completed and cache:
        _workable_cache = cache
        _workable_titles.clear()
        _workable_titles.update(titles)
        _workable_feed_ok = True
    elif _workable_cache is None:
        _workable_cache = {}  # first-ever pull failed; register no boards
        _workable_feed_ok = False
    return dict(_workable_titles)


def _wk_fetch_records(board: dict[str, Any], use_etag: bool) -> dict[str, Any]:
    if _workable_cache is None:
        load_workable_feed()
    if not _workable_feed_ok:
        # No trustworthy feed this run — leave the board's jobs exactly as they are.
        return {"status": "unchanged"}
    raw = (_workable_cache or {}).get(board["slug"], [])
    normalized = [n for job in raw if (n := _wk_normalize(job))]
    return {"status": "ok", "normalized": normalized, "etag": None}


def _wk_probe(slug: str) -> bool:
    if _workable_cache is None:
        load_workable_feed()
    return bool(_workable_feed_ok) and slug in (_workable_cache or {})


# Registered platforms beyond the upstream three. `fetch_records` yields the
# same normalized shape the upstream normalize pass produces; `describe` fills a
# missing description for a promising posting; `probe` validates a slug guess.
EXTRA_ATS: dict[str, dict[str, Any]] = {
    "smartrecruiters": {
        "fetch_records": _sr_fetch_records,
        "describe": _sr_describe,
        "probe": _sr_probe,
        "domains": ["jobs.smartrecruiters.com", "careers.smartrecruiters.com"],
    },
    "workable": {
        "fetch_records": _wk_fetch_records,
        "describe": lambda *_: "",  # descriptions are inline in the feed
        "probe": _wk_probe,
        "domains": ["apply.workable.com", "jobs.workable.com"],
        "feed": load_workable_feed,  # primed once per sweep by ingestion
    },
}


def describe(ats: str, slug: str, job_id: str) -> str:
    fn = EXTRA_ATS.get(ats, {}).get("describe")
    return fn(slug, job_id) if fn else ""


def fetch_records(board: dict[str, Any], use_etag: bool) -> dict[str, Any]:
    return EXTRA_ATS[board["ats"]]["fetch_records"](board, use_etag)


# --------------------------------------------------------------------------- #
# Directed discovery
# --------------------------------------------------------------------------- #


def load_curated_companies() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_COMPANY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in payload.get("companies", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            out.append(entry)
    return out


def slug_candidates(name: str, extra: Iterable[str] = (), limit: int = 5) -> list[str]:
    lowered = _SLUG_NOISE.sub(" ", name.lower()).strip()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    dashed = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    ordered: list[str] = []
    for cand in [*extra, compact, dashed, f"{compact}hq", f"{compact}careers", f"{compact}india"]:
        cand = (cand or "").strip()
        if cand and len(cand) >= 2 and cand not in ordered:
            ordered.append(cand)
    return ordered[:limit]


def probe_for(ats: str) -> Callable[[str], bool]:
    """A `slug -> bool` existence check for one ATS (HEAD for the upstream three,
    an India-postings count for SmartRecruiters)."""
    if ats in EXTRA_ATS:
        return EXTRA_ATS[ats]["probe"]
    return lambda slug: _upstream.board_exists(ats, slug)


def discover_indian_boards(
    known: dict[str, set[str]],
    ats_list: Iterable[str],
    concurrency: int = 8,
    per_company: int = 5,
) -> dict[str, list[str]]:
    """Probe ATS endpoints with slug guesses from the curated company roster.

    `known` maps ats -> set of slugs already in the database (lowercased); those
    are skipped. Returns ats -> sorted list of newly confirmed slugs.
    """
    companies = load_curated_companies()
    ats_list = list(ats_list)
    tasks: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for entry in companies:
        cands = slug_candidates(entry["name"], entry.get("slugs") or [], per_company)
        for ats in ats_list:
            for cand in cands:
                key = (ats, cand.lower())
                if cand.lower() in known.get(ats, set()) or key in seen:
                    continue
                seen.add(key)
                tasks.append((ats, cand))

    if not tasks:
        return {}

    def _check(item: tuple[str, str]) -> tuple[str, str, bool]:
        ats, cand = item
        try:
            return ats, cand, probe_for(ats)(cand)
        except Exception:
            return ats, cand, False

    found: dict[str, set[str]] = {ats: set() for ats in ats_list}
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for ats, cand, ok in pool.map(_check, tasks):
            if ok:
                found[ats].add(cand)
    return {ats: sorted(slugs) for ats, slugs in found.items() if slugs}
