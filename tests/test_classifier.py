from app.classifier import (
    classify_job,
    display_company,
    extract_experience,
    extract_salary,
    extract_skills,
    is_known_indian_company,
    is_software_role,
    location_is_india,
    plain_text,
)


def test_plain_text_removes_markup_and_decodes_entities():
    assert plain_text("<p>Build&nbsp;APIs &amp; tools</p>") == "Build APIs & tools"
    assert plain_text("Build\x00APIs") == "Build APIs"


def test_indian_city_location_is_detected():
    assert location_is_india("Bengaluru, Karnataka")
    assert location_is_india("Gurgaon / Hybrid")
    assert not location_is_india("Berlin, Germany")


def test_software_role_rejects_non_software_engineering():
    assert is_software_role("Backend Software Engineer")
    assert is_software_role("Engineer", team="Cloud Platform", description="Build APIs in Python")
    assert not is_software_role("Mechanical Engineer", description="Manufacturing systems")
    assert not is_software_role("Sales Engineer", description="Meet quarterly revenue targets")


def test_experience_range_and_plus_requirement():
    assert extract_experience("You have 0-2 years of relevant experience") == (0.0, 2.0, True)
    assert extract_experience("Minimum of 3+ years of software experience") == (3.0, None, True)


def test_total_experience_wins_over_smaller_skill_requirement():
    text = "5+ years of software experience and 2+ years of relevant experience with Kubernetes"
    assert extract_experience(text) == (5.0, None, True)


def test_entry_role_gets_high_priority_score():
    result = classify_job(
        title="Graduate Software Engineer",
        description="Freshers are welcome. Work with Python and PostgreSQL.",
        location="Hyderabad, India",
        board_slug="example",
        board_is_india=False,
        is_remote=False,
    )
    assert result.is_target
    assert result.experience_level == "entry"
    assert result.entry_level_score >= 85
    assert {"Python", "PostgreSQL"}.issubset(result.skills)


def test_senior_role_is_kept_but_deprioritized():
    result = classify_job(
        title="Senior Backend Software Engineer",
        description="Requires at least 7 years of professional experience.",
        location="Pune, India",
        board_slug="example",
        board_is_india=False,
        is_remote=False,
    )
    assert result.is_target
    assert result.experience_level == "senior"
    assert result.entry_level_score <= 10


def test_sde_roman_numerals_map_to_expected_levels():
    entry = classify_job(
        title="Software Development Engineer I",
        description="Build APIs.",
        location="India",
        board_slug="example",
        board_is_india=False,
        is_remote=False,
    )
    senior = classify_job(
        title="Software Development Engineer III - Backend",
        description="Build APIs.",
        location="India",
        board_slug="example",
        board_is_india=False,
        is_remote=False,
    )
    assert entry.experience_level == "entry"
    assert senior.experience_level == "senior"


def test_two_year_requirement_is_still_early_career():
    result = classify_job(
        title="Machine Learning Engineer",
        description="At least 2 years of professional experience.",
        location="Remote - India",
        board_slug="example",
        board_is_india=False,
        is_remote=True,
    )
    assert result.experience_level == "entry"


def test_known_indian_company_can_supply_remote_global_role():
    assert is_known_indian_company("razorpay")
    result = classify_job(
        title="Junior Software Developer",
        description="Build merchant APIs.",
        location="Remote",
        board_slug="razorpay",
        board_is_india=True,
        is_remote=True,
    )
    assert result.is_target
    assert result.india_match_reason == "indian_company"


def test_skill_boundaries_do_not_treat_ordinary_go_as_golang():
    skills = extract_skills("Software Engineer", "You will go to customer sites and write JavaScript")
    assert "Go" not in skills
    assert "JavaScript" in skills


def test_inr_lpa_salary_is_normalized_to_annual_rupees():
    assert extract_salary("Compensation: INR 8-12 LPA") == (
        800_000.0,
        1_200_000.0,
        "INR",
        "year",
    )


def test_company_display_name_is_readable():
    assert display_company("example-company") == "Example Company"
