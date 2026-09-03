# Coverage Analysis & Expansion

Snapshot date: 2026-09-03. Numbers are from the production database.

## The funnel, before this work

```
13,334 boards  →  621 productive (4.7%)  →  2,942 active jobs  →  114 early-career
```

| Stage | Value | Note |
|---|---|---|
| Boards total | 13,334 | 98.9% from one dated archive import; discovery adding ~0–84/run |
| Boards returning any posting | 11,238 | 2,096 dead/empty career pages |
| Boards contributing ≥1 active job | **621** | 95% of boards yield nothing for India |
| Active jobs | 2,942 | greenhouse 1,801 / lever 672 / ashby 469 |
| — `india_location` | 2,911 (99%) | pure location-string match |
| — `indian_company` | 30 | |
| — `remote_from_india` | **1** | path was structurally broken |
| `is_india_company` boards | **8** | of 13,334 |
| Experience mix | senior 69% · unknown 21% · mid 6% · **entry 3% · internship 0.7%** | GCC-dominated |
| Freshness | 41% of active jobs older than 90 days; oldest 2019 | no age cap |

**Diagnosis.** The 13k boards are global; the classifier correctly discards the
overwhelmingly non-India majority. The feed that remains is ~98% multinational
*Global Capability Centre* roles (Okta, Databricks, GitLab, Zscaler, DigitalOcean…
hiring in Bengaluru/Hyderabad), which skew senior. The genuine gaps:

1. **Only 3 ATS platforms**, none of them where Indian product companies concentrate.
2. **`is_india_company` under-flagged** — 13 of 19 probe-confirmed India-heavy boards
   (InMobi, Glance, Turing, HackerRank, Zeta, Porter…) were unflagged, so their
   geography-neutral remote roles were dropped.
3. **`remote_from_india` broken** — Greenhouse (61% of boards) only fetched a
   posting's description when India was *already* matched, but the remote-eligibility
   check *needs* that description. Circular; hence 1 job.
4. **Stale postings** dilute the feed and the stats.
5. **Discovery stalled** — the 3 ATS domains are mined out of Wayback/urlscan;
   `boards_discovered` is ~0 most runs.

## Contained tests run before committing

All throwaway HTTP probes, no writes. Full log: the commit that added this file.

| Source | Verdict | Evidence |
|---|---|---|
| **SmartRecruiters** | **integrated** | `api.smartrecruiters.com/v1/companies/{slug}/postings` — public, no auth, `?country=in` filter (Bosch 4799→546, Swiggy 69), rich shape, `jobAd.sections` HTML descriptions, apply URL `jobs.smartrecruiters.com/{id}` (200), archive-discoverable (Wayback 30d → 289 candidates). Blind slug guesses found Swiggy (69 IN), Freshworks (34 IN of 157), ixigo, Unacademy, Whatfix, Cars24, Upstox, Lendingkart, Refyne, MindTickle, HackerRank — all net-new. |
| **Workable** | **integrated (feed)** | The *widget* endpoint (`apply.workable.com/api/v1/widget/accounts/{slug}`) is dead for most accounts, and Workable is an embedded widget so its boards are invisible to web archives — but `jobs.workable.com/api/v1/jobs?location=india` is a **public marketplace API**: 3,811 India jobs, full HTML descriptions inline, `company.title`, `workplace` (on_site/hybrid/remote — 874 remote), pagination via `nextPageToken`. 313 distinct companies incl. Apna (161), 2070Health (111), Innovaccer Analytics, Exponent Energy, wati.io, Lokal, Lakshya Digital. One ~2-min sequential feed pull per discovery run; no per-board HTTP. |
| **Directed slug-probing** | **integrated** | 29/123 curated Indian companies got a board hit with naive slug-gen (~24%); most net-new hits were SmartRecruiters. Zero curated companies were on the Workable *widget*. |
| Recruitee | deferred | `{slug}.recruitee.com/api/offers` → "Not Found" for known customers; API gated. EU-centric. |
| Keka / Freshteam / Zoho Recruit | deferred | No stable unauthenticated JSON endpoint. |
| **Workday** | **follow-up — biggest remaining** | `{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` reachable (HTTP 422, not 404) but needs per-tenant `(tenant, wdN, site)` discovery + correct POST body / anti-bot handling. This is where Flipkart, PhonePe, Zomato and most GCC early-career volume lives. |
| urlscan `page.country:IN` | dead idea | Returns 0 for every ATS domain — API/SPA pages carry no geo tag. |

## What changed (this branch)

### Tier 1 — supply

- **`app/sources.py`** — new module. A native **SmartRecruiters** adapter (pagination,
  `?country=in`, on-demand `jobAd` description fetch); a **Workable** feed source
  (pull `jobs.workable.com/api/v1/jobs?location=india` once, cache by company, serve
  each board from cache — no per-board HTTP); and **directed discovery**: probe each
  per-slug ATS endpoint with slug candidates from a curated roster. Reuses
  `upstream.fetch` so the User-Agent / retry / backoff contract is unchanged.
- **`_prime_feed_sources()`** runs before every sweep. It re-pulls the Workable feed
  on the daily/monthly discovery runs (~2 min, sequential); incremental sweeps reuse
  that cache. It registers a `job_boards` row per Workable company.
- **`data/indian-companies.json`** — ~320-company curated roster of Indian software
  employers (`name` + ATS slug candidates). Feeds both `classifier.is_known_indian_company`
  (slug hints: 40 → 427 normalized) and directed discovery.
- **`data/india-boards.seed.json`** — added a `smartrecruiters` key and more
  confirmed India-heavy Ashby/Greenhouse/Lever slugs.
- **Schema v3** — `ats` CHECK on `job_boards` and `jobs` widened to include
  `smartrecruiters` and `workable` via an idempotent, target-list-driven migration;
  new `job_boards.last_discovered_at`.
- **`is_india_company` auto-promotion** (`_promote_india_companies`, every sweep) —
  a board with ≥3 India-located active postings, or ≥40% of ≥2, is flagged. On the
  current data this promotes **~415 boards** (8 → ~423), unlocking their remote roles.
- **`refresh_boards`** now runs archive discovery **and** directed discovery, and on
  the monthly full run resurrects boards that previously 404'd (`_resurrect_dead_boards`).

### Tier 2 — remote-from-India

- Descriptions are now fetched for **any** prelim-software posting that is
  India-linked *or remote* (was: India-linked only), so the remote-eligibility
  check can actually run. Applies to Greenhouse and SmartRecruiters.
- `_REMOTE_INDIA_ELIGIBILITY_RE` gained real phrasings: "India-based", "Pan-India",
  "anywhere in India", "hiring across India", "eligible to work in India",
  "must reside in India", "based out of <city>". The overseas-office guard
  (`test_generic_india_mention_still_rejected_for_overseas_remote_role`) still holds.

### Tier 3 — freshness

- `_close_stale_jobs` — on the monthly unconditional sweep, close still-listed
  postings whose publish date is older than `STALE_JOB_DAYS` (120). Preview on
  current data: **988 jobs**. A re-posted role returns with a fresh id.
- Dead boards are retried monthly instead of being removed permanently.

### Tier 4 — measurement

- **`GET /api/admin/coverage`** (token-protected) returns the whole funnel:
  board totals + per-ATS productivity, jobs by `india_match_reason` and
  `experience_level`, early-career count, freshness (median age, older-than-90d),
  and the last 12 runs' `boards_discovered` / `jobs_targeted`.

## Expected effect

- **Workable**: **313 company boards / 3,811 raw India jobs** enter the pipeline (of
  which the software classifier keeps an estimated 800–1,200). 874 of the raw jobs are
  remote. Apna, 2070Health, Innovaccer Analytics, Exponent Energy, wati.io, Lokal…
- **SmartRecruiters**: +tens of India-heavy boards from seed + discovery; Swiggy,
  Freshworks, ixigo, Unacademy, HackerRank, Refyne, Lendingkart et al. enter the feed.
- **Promotion**: ~415 boards start contributing their remote India-eligible roles.
- **Remote path**: `remote_from_india` should move from 1 into the hundreds once
  descriptions are available to classify (and Workable's `workplace: remote` roles
  carry India location directly).
- **Freshness**: ~1,000 stale postings drop out on the next full run; median age falls.

Rough combined ceiling: the active-job count should move from ~2,900 toward the
5,000–7,000 range after the first full discovery run, with early-career finally in
the several-hundreds rather than 114.

## Still open

- **Workday adapter** — the single largest remaining source of Indian early-career
  and GCC volume. Needs a tenant-discovery strategy.
- **Aggregators** (Wellfound, Instahyre, Naukri) — different data model, ToS review.
- **Better slug generation** for directed discovery — current naive generator misses
  legal-entity slugs (e.g. Razorpay's `razorpaysoftwareprivatelimited`).
- The curated roster should be regenerated periodically from NASSCOM / Tracxn / YC lists.
