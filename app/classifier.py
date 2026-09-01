from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html import unescape
from typing import Any


_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")

INDIA_TERMS = (
    "india",
    "bengaluru",
    "bangalore",
    "hyderabad",
    "gurugram",
    "gurgaon",
    "noida",
    "new delhi",
    "delhi ncr",
    "mumbai",
    "pune",
    "chennai",
    "kolkata",
    "ahmedabad",
    "jaipur",
    "kochi",
    "cochin",
    "thiruvananthapuram",
    "trivandrum",
    "chandigarh",
    "indore",
    "bhubaneswar",
    "coimbatore",
    "nagpur",
    "vadodara",
    "goa",
)

CITY_NAMES = {
    term: term.title()
    for term in INDIA_TERMS
    if term not in {"india", "delhi ncr"}
}
CITY_NAMES.update(
    {
        "bangalore": "Bengaluru",
        "gurgaon": "Gurugram",
        "new delhi": "Delhi NCR",
        "delhi ncr": "Delhi NCR",
        "cochin": "Kochi",
        "trivandrum": "Thiruvananthapuram",
    }
)

# Exact, normalized board hints. Location evidence discovered during scans promotes
# additional boards in PostgreSQL, so this is a bootstrap list rather than a ceiling.
INDIAN_COMPANY_HINTS = {
    "apna",
    "browserstack",
    "cashfree",
    "chargebee",
    "clevertap",
    "coinswitch",
    "cred",
    "darwinbox",
    "delhivery",
    "dream11",
    "freshworks",
    "games24x7",
    "groww",
    "hasura",
    "innovaccer",
    "ixigo",
    "makemytrip",
    "meesho",
    "mindtickle",
    "moengage",
    "ninjacart",
    "ofbusiness",
    "ola",
    "oyo",
    "paytm",
    "phonepe",
    "postman",
    "razorpay",
    "razorpaysoftwareprivatelimited",
    "sharechat",
    "slice",
    "smallcase",
    "swiggy",
    "udaan",
    "urbancompany",
    "upstox",
    "whatfix",
    "yellowai",
    "zetwerk",
    "zerodha",
    "zomato",
}
NORMALIZED_INDIAN_COMPANY_HINTS = {
    re.sub(r"[^a-z0-9]", "", hint.lower()) for hint in INDIAN_COMPANY_HINTS
}

SOFTWARE_TITLE_RE = re.compile(
    r"\b(?:"
    r"software|developer|programmer|backend|back[ -]?end|frontend|front[ -]?end|"
    r"full[ -]?stack|web engineer|mobile engineer|android|ios|platform engineer|"
    r"site reliability|\bsre\b|devops|devsecops|cloud engineer|infrastructure engineer|"
    r"data engineer|analytics engineer|machine learning engineer|ml engineer|"
    r"ai engineer|artificial intelligence|security engineer|application security|"
    r"qa engineer|quality assurance|test automation|automation engineer|"
    r"embedded software|firmware engineer|solutions architect|engineering manager|"
    r"technical architect|member of technical staff|\bmts\b|\bsde(?:\s*[i1-3]+)?\b"
    r")\b",
    re.IGNORECASE,
)
GENERIC_ENGINEER_RE = re.compile(r"\bengineer(?:ing)?\b", re.IGNORECASE)
SOFTWARE_CONTEXT_RE = re.compile(
    r"\b(?:api|microservices?|distributed systems?|web application|saas|cloud|"
    r"database|backend|frontend|fullstack|python|java(?:script)?|typescript|react|"
    r"node(?:\.js)?|golang|kubernetes|docker|aws|azure|gcp|sql|codebase|coding)\b",
    re.IGNORECASE,
)
NON_SOFTWARE_TITLE_RE = re.compile(
    r"\b(?:mechanical|civil|chemical|electrical|manufacturing|industrial|biomedical|"
    r"field service|sales engineer|customer success|support engineer|network engineer|"
    r"hardware validation|vlsi|semiconductor|physical design)\b",
    re.IGNORECASE,
)

ENTRY_TITLE_RE = re.compile(
    r"\b(?:intern(?:ship)?|apprentice|graduate|new grad|fresher|trainee|junior|"
    r"entry[ -]?level|associate|software (?:development )?engineer\s+(?:i|1)|"
    r"sde\s*(?:i|1))\b",
    re.IGNORECASE,
)
INTERNSHIP_TITLE_RE = re.compile(r"\b(?:intern(?:ship)?|apprentice)\b", re.IGNORECASE)
MID_TITLE_RE = re.compile(
    r"\b(?:mid[ -]?level|intermediate|software (?:development )?engineer\s+(?:ii|2)|"
    r"sde\s*(?:ii|2))\b",
    re.IGNORECASE,
)
SENIOR_TITLE_RE = re.compile(
    r"\b(?:senior|sr\.?|staff|principal|lead|manager|director|head|architect|"
    r"distinguished|software (?:development )?engineer\s+(?:iii|3)|"
    r"sde\s*(?:iii|3))\b",
    re.IGNORECASE,
)

EXPERIENCE_PATTERNS = (
    re.compile(
        r"(?<!\d)(\d{1,2}(?:\.5)?)\s*(?:[-–—]|to)\s*(\d{1,2}(?:\.5)?)\s*\+?\s*"
        r"(?:years?|yrs?)(?:\s+of)?(?:\s+(?:relevant|professional|work|industry|software|development))?\s+experience",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:minimum|min\.?|at least)\s*(?:of\s*)?(\d{1,2}(?:\.5)?)\s*\+?\s*"
        r"(?:years?|yrs?)(?:\s+of)?(?:\s+(?:relevant|professional|work|industry|software|development))?\s+experience",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d\-–—])(\d{1,2}(?:\.5)?)\s*\+\s*(?:years?|yrs?)(?:\s+of)?\s*"
        r"(?:relevant|professional|work|industry|software|development|engineering)?\s*experience",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<![\d\-–—])(\d{1,2}(?:\.5)?)\s*(?:years?|yrs?)(?:\s+of)?\s*"
        r"(?:relevant|professional|work|industry|software|development|engineering)\s+experience",
        re.IGNORECASE,
    ),
)

SKILL_PATTERNS: tuple[tuple[str, str], ...] = (
    ("Python", r"\bpython\b"),
    ("Java", r"\bjava\b(?!script)"),
    ("JavaScript", r"\bjavascript\b|\bjs\b"),
    ("TypeScript", r"\btypescript\b"),
    ("React", r"\breact(?:\.js|js)?\b"),
    ("Angular", r"\bangular\b"),
    ("Vue", r"\bvue(?:\.js|js)?\b"),
    ("Node.js", r"\bnode(?:\.js|js)\b"),
    ("Go", r"\bgolang\b|\bgo programming\b"),
    ("Rust", r"\brust\b"),
    ("C++", r"(?<!\w)c\+\+(?!\w)"),
    ("C#", r"(?<!\w)c#(?!\w)"),
    (".NET", r"(?<!\w)\.net\b|\bdotnet\b"),
    ("Kotlin", r"\bkotlin\b"),
    ("Swift", r"\bswift\b"),
    ("Android", r"\bandroid\b"),
    ("iOS", r"\bios\b"),
    ("SQL", r"\bsql\b"),
    ("PostgreSQL", r"\bpostgres(?:ql)?\b"),
    ("MySQL", r"\bmysql\b"),
    ("MongoDB", r"\bmongodb\b"),
    ("Redis", r"\bredis\b"),
    ("AWS", r"\baws\b|amazon web services"),
    ("Azure", r"\bazure\b"),
    ("GCP", r"\bgcp\b|google cloud"),
    ("Docker", r"\bdocker\b"),
    ("Kubernetes", r"\bkubernetes\b|\bk8s\b"),
    ("Terraform", r"\bterraform\b"),
    ("Linux", r"\blinux\b"),
    ("Git", r"\bgit\b"),
    ("Spring", r"\bspring boot\b|\bspring framework\b"),
    ("Django", r"\bdjango\b"),
    ("FastAPI", r"\bfastapi\b"),
    ("Flask", r"\bflask\b"),
    ("Ruby", r"\bruby\b"),
    ("Rails", r"\bruby on rails\b|\brails\b"),
    ("PHP", r"\bphp\b"),
    ("GraphQL", r"\bgraphql\b"),
    ("Kafka", r"\bkafka\b"),
    ("Spark", r"\bapache spark\b|\bpyspark\b"),
    ("Airflow", r"\bairflow\b"),
    ("Databricks", r"\bdatabricks\b"),
    ("Snowflake", r"\bsnowflake\b"),
    ("PyTorch", r"\bpytorch\b"),
    ("TensorFlow", r"\btensorflow\b"),
    ("LLMs", r"\bllms?\b|large language models?"),
    ("Machine Learning", r"\bmachine learning\b"),
    ("CI/CD", r"\bci\s*/\s*cd\b|continuous integration"),
)
COMPILED_SKILLS = tuple((name, re.compile(pattern, re.IGNORECASE)) for name, pattern in SKILL_PATTERNS)


@dataclass(frozen=True)
class Classification:
    is_target: bool
    india_match_reason: str
    city: str | None
    experience_min: float | None
    experience_max: float | None
    experience_level: str
    experience_is_explicit: bool
    entry_level_score: int
    skills: list[str]
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    salary_period: str | None


def plain_text(value: str | None) -> str:
    if not value:
        return ""
    # PostgreSQL text rejects NUL bytes; public descriptions occasionally contain
    # malformed control characters copied from rich-text editors.
    value = value.replace("\x00", " ")
    return _SPACE_RE.sub(" ", unescape(_TAG_RE.sub(" ", value))).strip()


def normalize_company_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def display_company(slug: str) -> str:
    spaced = re.sub(r"[-_]+", " ", slug).strip()
    return " ".join(word.upper() if len(word) <= 3 else word.capitalize() for word in spaced.split())


def is_known_indian_company(slug: str) -> bool:
    normalized = normalize_company_slug(slug)
    return normalized in NORMALIZED_INDIAN_COMPANY_HINTS


def location_is_india(location: str) -> bool:
    lowered = plain_text(location).lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in INDIA_TERMS)


def extract_city(location: str) -> str | None:
    lowered = plain_text(location).lower()
    for term in sorted(CITY_NAMES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(term)}\b", lowered):
            return CITY_NAMES[term]
    return None


def india_match(
    location: str,
    description: str,
    board_slug: str,
    board_is_india: bool,
    is_remote: bool,
) -> tuple[bool, str]:
    if location_is_india(location):
        return True, "india_location"
    description_l = plain_text(description).lower()
    if is_remote and any(
        re.search(rf"\b{re.escape(term)}\b", description_l) for term in INDIA_TERMS
    ):
        return True, "remote_from_india"
    if board_is_india or is_known_indian_company(board_slug):
        return True, "indian_company"
    return False, ""


def is_software_role(title: str, department: str = "", team: str = "", description: str = "") -> bool:
    title = plain_text(title)
    if NON_SOFTWARE_TITLE_RE.search(title) and not re.search(
        r"\b(?:software|developer|firmware|embedded software)\b", title, re.IGNORECASE
    ):
        return False
    if SOFTWARE_TITLE_RE.search(title):
        return True
    context = " ".join((department, team, plain_text(description)[:5000]))
    return bool(GENERIC_ENGINEER_RE.search(title) and SOFTWARE_CONTEXT_RE.search(context))


def extract_experience(text: str) -> tuple[float | None, float | None, bool]:
    clean = plain_text(text)
    candidates: list[tuple[float, float | None]] = []
    for index, pattern in enumerate(EXPERIENCE_PATTERNS):
        for match in pattern.finditer(clean):
            minimum = float(match.group(1))
            maximum = float(match.group(2)) if index == 0 and match.lastindex and match.lastindex > 1 else None
            if minimum <= 20 and (maximum is None or minimum <= maximum <= 25):
                candidates.append((minimum, maximum))
    if not candidates:
        zero_to_two = re.search(
            r"\b(?:no prior experience|freshers? (?:are )?(?:welcome|eligible)|0\s*[-–]\s*2\s+years?)\b",
            clean,
            re.IGNORECASE,
        )
        return (0.0, 2.0, True) if zero_to_two else (None, None, False)
    # Requirements often contain a total-experience requirement plus smaller skill-
    # specific requirements. The largest minimum best represents the gating total.
    minimum, maximum = max(candidates, key=lambda item: item[0])
    return minimum, maximum, True


def classify_experience(
    title: str, minimum: float | None, maximum: float | None
) -> tuple[str, int]:
    if INTERNSHIP_TITLE_RE.search(title):
        return "internship", 100
    senior = bool(SENIOR_TITLE_RE.search(title))
    entry = bool(ENTRY_TITLE_RE.search(title))
    mid = bool(MID_TITLE_RE.search(title))

    if senior or (minimum is not None and minimum >= 5):
        level = "senior"
    elif mid:
        level = "mid"
    elif entry or (maximum is not None and maximum <= 2) or (
        minimum is not None and minimum <= 2
    ):
        level = "entry"
    elif minimum is not None and minimum >= 2:
        level = "mid"
    else:
        level = "unknown"

    score = 48
    if entry:
        score += 42
    if minimum is not None:
        if minimum <= 1:
            score += 22
        elif minimum <= 2:
            score += 8
        elif minimum >= 5:
            score -= 45
        elif minimum >= 3:
            score -= 25
    if maximum is not None and maximum <= 2:
        score += 12
    if mid:
        score -= 22
    if senior:
        score -= 65
    if level == "internship":
        score = 100
    return level, max(0, min(100, score))


def extract_skills(title: str, description: str) -> list[str]:
    text = f"{plain_text(title)} {plain_text(description)}"
    return [name for name, pattern in COMPILED_SKILLS if pattern.search(text)][:12]


def extract_salary(text: str) -> tuple[float | None, float | None, str | None, str | None]:
    clean = plain_text(text)
    inr_lpa = re.search(
        r"(?:₹|INR\s*)?\s*(\d+(?:\.\d+)?)\s*(?:[-–—]|to)\s*(\d+(?:\.\d+)?)\s*(?:LPA|lakhs? per annum)",
        clean,
        re.IGNORECASE,
    )
    if inr_lpa:
        return (
            float(inr_lpa.group(1)) * 100_000,
            float(inr_lpa.group(2)) * 100_000,
            "INR",
            "year",
        )
    usd = re.search(
        r"\$\s*([\d,]+)\s*(?:[-–—]|to)\s*\$?\s*([\d,]+)\s*(?:per\s+)?(year|month|hour|annum)?",
        clean,
        re.IGNORECASE,
    )
    if usd:
        return (
            float(usd.group(1).replace(",", "")),
            float(usd.group(2).replace(",", "")),
            "USD",
            (usd.group(3) or "year").lower().replace("annum", "year"),
        )
    return None, None, None, None


def classify_job(
    *,
    title: str,
    description: str,
    location: str,
    board_slug: str,
    board_is_india: bool,
    is_remote: bool,
    department: str = "",
    team: str = "",
) -> Classification:
    software = is_software_role(title, department, team, description)
    india, reason = india_match(
        location, description, board_slug, board_is_india, is_remote
    )
    minimum, maximum, explicit = extract_experience(f"{title}. {description}")
    level, score = classify_experience(title, minimum, maximum)
    salary_min, salary_max, currency, period = extract_salary(description)
    return Classification(
        is_target=software and india,
        india_match_reason=reason,
        city=extract_city(location),
        experience_min=minimum,
        experience_max=maximum,
        experience_level=level,
        experience_is_explicit=explicit,
        entry_level_score=score,
        skills=extract_skills(title, description),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=currency,
        salary_period=period,
    )


def content_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()
