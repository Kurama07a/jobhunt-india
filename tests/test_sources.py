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
