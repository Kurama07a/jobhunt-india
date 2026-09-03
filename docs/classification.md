# Classification & Enrichment

All logic lives in `app/classifier.py`. It is **pure and deterministic** — regex/keyword
rules, no network, no ML. `classify_job(**kwargs) -> Classification` is the single entry
point; it is called during ingestion (`_fetch_board`) and during offline
`reclassify_existing_jobs()`.

```python
Classification(
    is_target: bool,               # software AND india — the keep/drop decision
    india_match_reason: str,        # "" | india_location | remote_from_india | indian_company
    city: str | None,              # canonical Indian city name, if detected
    experience_min: float | None,  # years
    experience_max: float | None,  # years
    experience_level: str,         # internship | entry | mid | senior | unknown
    experience_is_explicit: bool,  # True when a number was actually parsed from the text
    entry_level_score: int,        # 0..100, higher = better for early career
    skills: list[str],             # up to 12 canonical skill names
    salary_min / salary_max: float | None,
    salary_currency: str | None,   # "INR" | "USD"
    salary_period: str | None,     # "year" | "month" | "hour"
)
```

A posting is **kept only if `is_target` is true**, i.e. it is *both* a software role *and*
India‑relevant.

---

## 1. Is it a software role? (`is_software_role`)

Input: `title`, plus `department`, `team`, `description` as context.

1. **Hard exclude**: if the title matches `NON_SOFTWARE_TITLE_RE` (mechanical, civil,
   chemical, electrical, manufacturing, industrial, biomedical, field service, *sales
   engineer*, *customer success*, *support engineer*, *network engineer*, hardware
   validation, VLSI, semiconductor, physical design) **and** does not also contain
   `software|developer|firmware|embedded software` → **not** software.
2. **Direct match**: title matches `SOFTWARE_TITLE_RE` (software, developer, programmer,
   backend/frontend/full‑stack, web/mobile engineer, android, ios, platform engineer,
   SRE / site reliability, devops/devsecops, cloud/infrastructure engineer, data /
   analytics / ML / AI engineer, security / application security engineer, QA / test
   automation, embedded / firmware, solutions architect, engineering manager, technical
   architect, member of technical staff / MTS, `SDE` with optional I/II/III) → software.
3. **Contextual match**: title has a bare `engineer`/`engineering` **and** the combined
   department+team+first 5,000 chars of description matches `SOFTWARE_CONTEXT_RE`
   (api, microservices, distributed systems, web application, saas, cloud, database,
   backend/frontend/fullstack, python, java(script), typescript, react, node, golang,
   kubernetes, docker, aws/azure/gcp, sql, codebase, coding) → software.
4. Otherwise → not software.

---

## 2. Is it India‑relevant? (`india_match`)

Checked in order; first hit wins and sets `india_match_reason`:

| Reason | Condition |
|---|---|
| `india_location` | `location_is_india(location)` — the location string contains a whole‑word match for any term in `INDIA_TERMS` (`india` + ~30 city names incl. Bengaluru/Bangalore, Hyderabad, Gurugram/Gurgaon, Noida, Delhi NCR, Mumbai, Pune, Chennai, Kolkata, Ahmedabad, Jaipur, Kochi/Cochin, Thiruvananthapuram/Trivandrum, Chandigarh, Indore, Bhubaneswar, Coimbatore, Nagpur, Vadodara, Goa). |
| `remote_from_india` | `is_remote` is true **and** `remote_description_allows_india(location, description)` — see below. |
| `indian_company` | The board is flagged `is_india_company` **or** the slug is in `INDIAN_COMPANY_HINTS` (normalized). |
| `""` (drop) | none of the above. |

### `remote_from_india` — the strict remote gate

This exists to stop "global remote" roles tied to an overseas office from leaking in on a
generic "we have an India office" mention.

Two conditions must **both** hold:

1. **The location is genuinely geography‑neutral.** After lowercasing and pulling out
   alphabetic tokens, every token must be in `_GENERIC_REMOTE_LOCATION_TOKENS`
   (`anywhere`, `apac`, `asia`, `remote`, `global`, `worldwide`, `distributed`,
   `flexible`, `home`, `hybrid`, `virtual`, `pacific`, `multiple`, `location(s)`,
   `only`, `based`, …), or the location is empty. A location like `San Francisco-HQ`
   fails here.
2. **The description explicitly describes India eligibility.**
   `_REMOTE_INDIA_ELIGIBILITY_RE` must match — e.g. "based/located/residing in
   <India term>", "work(ing) from <India term>", "candidates/applicants must be based in
   <India term>", "open/available to candidates in <India term>", "remote – <India
   term>", or "<India term> – remote/only/based/residents". A bare "we have offices in
   India" does **not** match.

Covered by `test_remote_overseas_office_is_not_matched_by_generic_india_copy` and
`test_generic_remote_role_requires_explicit_india_eligibility`.

### Indian company recognition

`INDIAN_COMPANY_HINTS` (`app/classifier.py:105`) is a bootstrap set of ~40 well‑known
Indian company slugs (razorpay, swiggy, zomato, cred, meesho, groww, phonepe, paytm,
zerodha, freshworks, postman, browserstack, …). Matching is done on the slug normalized
to `[a-z0-9]` only. This is a *floor*, not a ceiling: any board that produces an
`india_location` match during a scan gets `is_india_company` promoted to `true` in
PostgreSQL and stays that way.

### City extraction (`extract_city`)

Maps a detected term to a canonical display name via `CITY_NAMES` (Bangalore → Bengaluru,
Gurgaon → Gurugram, New Delhi → Delhi NCR, Cochin → Kochi, Trivandrum →
Thiruvananthapuram, others title‑cased). Stored in `jobs.city`.

---

## 3. Experience extraction (`extract_experience`)

Runs over `"{title}. {description}"`.

Four ordered regex patterns in `EXPERIENCE_PATTERNS`:

1. **Range**: `"3-5 years experience"`, `"3 to 5 yrs of experience"` → `(min, max)`.
2. **Minimum phrasing**: `"minimum 3 years experience"`, `"at least 3+ yrs …"` →
   `(min, None)`.
3. **Plus**: `"5+ years experience"` → `(min, None)`.
4. **Bare**: `"5 years of professional experience"` → `(min, None)`.

Sanity bounds: `min ≤ 20`, and for ranges `min ≤ max ≤ 25`; out‑of‑range candidates are
dropped.

If nothing matches, a fresher cue (`"no prior experience"`, `"freshers are
welcome/eligible"`, `"0-2 years"`) yields `(0.0, 2.0, explicit=True)`; otherwise
`(None, None, explicit=False)`.

When multiple numbers match (a total requirement plus smaller skill‑specific ones), the
**largest minimum** wins — it best represents the gating total. See
`test_total_experience_wins_over_smaller_skill_requirement`.

`experience_is_explicit` is `True` only when a number (or fresher cue) was actually
parsed. The API's `max_experience` filter only applies to explicit rows.

---

## 4. Experience level + early‑career score (`classify_experience`)

Title regexes: `INTERNSHIP_TITLE_RE`, `ENTRY_TITLE_RE` (intern, apprentice, graduate, new
grad, fresher, trainee, junior, entry‑level, associate, SWE I / SDE 1),
`MID_TITLE_RE` (mid‑level, intermediate, SWE II / SDE 2), `SENIOR_TITLE_RE` (senior, sr,
staff, principal, lead, manager, director, head, architect, distinguished, SWE III /
SDE 3).

**Level:**

| Result | Rule |
|---|---|
| `internship` | internship title (also forces score = 100) |
| `senior` | senior title, or `min ≥ 5` |
| `mid` | mid title, or (`min ≥ 2` and not entry/senior) |
| `entry` | entry title, or `max ≤ 2`, or `min ≤ 2` |
| `unknown` | nothing decisive |

**`entry_level_score`** starts at `48` and is adjusted, then clamped to `0..100`:

| Signal | Δ |
|---|---|
| entry title | `+42` |
| `min ≤ 1` | `+22` |
| `min ≤ 2` | `+8` |
| `min ≥ 5` | `−45` |
| `min ≥ 3` | `−25` |
| `max ≤ 2` | `+12` |
| mid title | `−22` |
| senior title | `−65` |
| internship | score set to `100` |

This score drives the default `sort=entry` ordering in the API.

---

## 5. Skills (`extract_skills`)

`COMPILED_SKILLS` is ~50 `(canonical name, regex)` pairs covering languages, frameworks,
datastores, cloud, infra, and ML tooling. Word‑boundary aware — e.g. `Java` excludes
`JavaScript`, `Go` requires `golang`/`go programming` (plain "go to customer sites"
doesn't match — `test_skill_boundaries_do_not_treat_ordinary_go_as_golang`). Returns the
first **12** matches over `"{title} {description}"`, stored as a Postgres `text[]`.

---

## 6. Salary (`extract_salary`)

Best‑effort, two patterns:

- **INR LPA**: `"INR 8-12 LPA"` / `"8 to 12 lakhs per annum"` → `min*100000`,
  `max*100000`, `INR`, `year`.
- **USD range**: `"$120,000 - $150,000 per year"` → parsed numbers, `USD`, and period
  (`year`/`month`/`hour`, `annum` → `year`).

No match → all four fields `None`. The dashboard renders `₹x–y LPA` for INR/year and a
formatted range otherwise.

---

## Reclassification

`reclassify_existing_jobs()` (`app/ingestion.py:349`, CLI `python -m app.cli reclassify`)
re‑runs `classify_job` over every active job using the **stored** title/description/
location — no network. Rows that are still `is_target` get their derived columns updated;
rows that no longer classify as a target are closed (`is_active = false`,
`closed_at = now()`). Returns `{scanned, updated, closed}`. Use it after editing
`classifier.py` so the change lands without a full ATS sweep.
