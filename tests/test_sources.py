import json
from pathlib import Path

from app import sources

ROOT = Path(__file__).resolve().parents[1]


def test_smartrecruiters_is_a_registered_extra_ats():
    assert "smartrecruiters" in sources.EXTRA_ATS
    entry = sources.EXTRA_ATS["smartrecruiters"]
    assert callable(entry["fetch_records"])
    assert callable(entry["describe"])
    assert callable(entry["probe"])
    assert entry["domains"]


def test_slug_candidates_keeps_explicit_slugs_first_and_strips_noise():
    cands = sources.slug_candidates("Acme Technologies India Pvt Ltd", ["acme"])
    assert cands[0] == "acme"
    assert "acmetechnologiesindiapvtltd" not in cands
    assert "acme" in cands
    # generated variants are lowercase alphanumerics / hyphenated
    assert all(c == c.lower() for c in cands)
    assert len(cands) <= 5


def test_slug_candidates_dedupes_and_bounds():
    cands = sources.slug_candidates("Zoho", ["zoho", "zoho", "zohocorp"], limit=3)
    assert cands[:2] == ["zoho", "zohocorp"]
    assert len(cands) == 3
    assert len(set(cands)) == len(cands)


def test_smartrecruiters_normalizer_maps_to_common_fields():
    posting = {
        "id": "123",
        "name": "Backend Engineer",
        "releasedDate": "2026-08-01T00:00:00.000Z",
        "location": {"city": "Bengaluru", "region": "KA", "country": "in", "remote": False, "hybrid": True},
        "typeOfEmployment": {"label": "Full-time"},
        "function": {"label": "Engineering"},
        "company": {"name": "Acme", "identifier": "AcmeInc"},
    }
    norm = sources._sr_normalize(posting, "acme")
    assert norm["id"] == "123"
    assert norm["title"] == "Backend Engineer"
    assert norm["location"] == "Bengaluru, KA, IN" or "Bengaluru" in norm["location"]
    assert norm["isRemote"] is False
    assert norm["workplaceType"] == "Hybrid"
    assert norm["employmentType"] == "Full-time"
    assert norm["jobUrl"] == "https://jobs.smartrecruiters.com/AcmeInc/123"
    assert norm["publishedAt"].startswith("2026-08-01")


def test_smartrecruiters_normalizer_marks_remote():
    norm = sources._sr_normalize(
        {"id": "9", "name": "SRE", "location": {"remote": True, "country": "in"}}, "x"
    )
    assert norm["isRemote"] is True
    assert norm["workplaceType"] == "Remote"


def test_workable_is_a_registered_feed_source():
    entry = sources.EXTRA_ATS["workable"]
    assert callable(entry["fetch_records"])
    assert callable(entry["probe"])
    assert callable(entry.get("feed")), "workable must expose a feed primer"


def test_workable_slug_is_taken_from_the_company_url():
    job = {
        "id": "x",
        "company": {"title": "Proximity Works", "id": "abc",
                    "url": "https://jobs.workable.com/company/xxx/jobs-at-proximity-works"},
    }
    assert sources._wk_slug(job) == "proximity-works"
    # falls back to company id when the url has no jobs-at segment
    assert sources._wk_slug({"id": "y", "company": {"id": "raw.id", "url": ""}}) == "raw.id"


def test_workable_normalizer_maps_fields_and_remote():
    job = {
        "id": "j1", "title": "Backend Engineer", "department": "Engineering",
        "employmentType": "Full-time", "workplace": "remote",
        "locations": ["Bengaluru, Karnataka, India"],
        "location": {"city": "Bengaluru", "subregion": "Karnataka", "countryName": "India"},
        "created": "2026-08-20T00:00:00.000Z",
        "url": "https://jobs.workable.com/view/abc/backend-engineer-at-acme",
        "description": "<p>Build APIs</p>",
        "company": {"title": "Acme"},
    }
    norm = sources._wk_normalize(job)
    assert norm["id"] == "j1"
    assert norm["title"] == "Backend Engineer"
    assert norm["isRemote"] is True
    assert norm["workplaceType"] == "Remote"
    assert norm["location"] == "Bengaluru, Karnataka, India"
    assert norm["_description"] == "<p>Build APIs</p>"
    assert norm["publishedAt"].startswith("2026-08-20")


def test_workable_feed_pages_and_caches(monkeypatch):
    pages = [
        {"jobs": [{"id": "1", "title": "SWE", "company": {"title": "A", "url": ".../jobs-at-acme"}}],
         "nextPageToken": "tok2"},
        {"jobs": [{"id": "2", "title": "SRE", "company": {"title": "A", "url": ".../jobs-at-acme"}},
                  {"id": "3", "title": "PM", "company": {"title": "B", "url": ".../jobs-at-beta"}}],
         "nextPageToken": None},
    ]
    calls = {"n": 0}

    class FakeUpstream:
        NotModified = NotFound = Exception
        def fetch(self, url, **kw):
            import json as _j
            i = calls["n"]; calls["n"] += 1
            return _j.dumps(pages[i]).encode()
        def plausible(self, slug, ats):
            return True

    monkeypatch.setattr(sources, "_upstream", FakeUpstream())
    monkeypatch.setattr(sources, "_workable_cache", None)
    monkeypatch.setattr(sources, "_workable_feed_ok", False)
    monkeypatch.setattr(sources, "WORKABLE_PAGE_DELAY", 0)
    sources._workable_titles.clear()

    titles = sources.load_workable_feed(force=True)
    assert calls["n"] == 2, "should follow nextPageToken then stop"
    assert set(titles) == {"acme", "beta"}
    assert sources._workable_feed_ok is True
    assert sources._wk_probe("acme") is True
    assert sources._wk_probe("nope") is False
    rec = sources._wk_fetch_records({"ats": "workable", "slug": "acme"}, False)
    assert rec["status"] == "ok" and len(rec["normalized"]) == 2


def test_workable_truncated_feed_does_not_commit_or_close_jobs(monkeypatch):
    class FailingUpstream:
        NotModified = NotFound = Exception
        def fetch(self, url, **kw):
            raise RuntimeError("429 throttled")
        def plausible(self, slug, ats):
            return True

    monkeypatch.setattr(sources, "_upstream", FailingUpstream())
    monkeypatch.setattr(sources, "_workable_cache", None)
    monkeypatch.setattr(sources, "_workable_feed_ok", False)
    sources.load_workable_feed(force=True)
    assert sources._workable_feed_ok is False
    # A board fetch must report "unchanged" so persist leaves its jobs alone.
    assert sources._wk_fetch_records({"ats": "workable", "slug": "acme"}, False)["status"] == "unchanged"


def test_curated_company_file_is_valid_and_substantial():
    payload = json.loads((ROOT / "data" / "indian-companies.json").read_text())
    companies = payload["companies"]
    assert len(companies) >= 150
    for entry in companies:
        assert isinstance(entry["name"], str) and entry["name"].strip()
        assert isinstance(entry.get("slugs", []), list)


def test_loader_returns_company_dicts():
    companies = sources.load_curated_companies()
    assert len(companies) >= 150
    assert all("name" in c for c in companies)
