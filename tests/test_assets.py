import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_n8n_workflow_is_valid_and_contains_production_schedules():
    workflow = json.loads((ROOT / "n8n/jobhunt-india-ingestion.json").read_text())[0]
    names = {node["name"] for node in workflow["nodes"]}
    assert {
        "Every 4 Hours",
        "Daily Recent Board Discovery",
        "Monthly Full Board Discovery",
        "Start Ingestion",
        "Get Run Status",
        "Fail Workflow",
    }.issubset(names)
    assert workflow["settings"]["timezone"] == "Europe/Berlin"


def test_dashboard_uses_no_third_party_runtime_assets():
    html = (ROOT / "app/static/index.html").read_text()
    head = html.split("</head>")[0]
    assert "https://" not in head
    assert "src=\"https://" not in html
    assert "/static/styles.css" in html
    assert "/static/app.js" in html


def test_schema_has_required_filter_indexes_and_unique_source_key():
    schema = (ROOT / "app/schema.sql").read_text()
    assert "UNIQUE (ats, source_job_id)" in schema
    assert "jobs_search_idx" in schema
    assert "jobs_skills_idx" in schema
    assert "ingestion_runs" in schema


def test_schema_widens_ats_constraint_for_new_platforms():
    schema = (ROOT / "app/schema.sql").read_text()
    assert "smartrecruiters" in schema
    # The idempotent migration block that widens an already-created constraint.
    assert "job_boards_ats_check" in schema
    assert "last_discovered_at" in schema


def test_extra_ats_platforms_are_wired_end_to_end():
    from app import sources

    # main's filter validation and the dashboard source picker must both know it.
    main_src = (ROOT / "app/main.py").read_text()
    assert '"smartrecruiters"' in main_src
    html = (ROOT / "app/static/index.html").read_text()
    assert 'value="smartrecruiters"' in html
    assert "smartrecruiters" in sources.EXTRA_ATS
