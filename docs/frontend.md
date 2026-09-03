# Web Dashboard

`app/static/` — `index.html`, `app.js`, `styles.css`. Served same‑origin by the FastAPI
app (`/` → `index.html`, `/static/*` → assets). **No build step, no framework, no CDN,
no third‑party runtime assets** — enforced by `tests/test_assets.py` and the app's CSP
(`script-src 'self'; style-src 'self'`). Fonts fall back to system UI fonts.

## Layout (`index.html`)

- **Topbar** — brand + a live status line (`#sync-label`) fed by `/api/sync-status`.
- **Hero** — static copy.
- **Metrics row** — four figures from `/api/stats`: `#metric-jobs` (active_jobs),
  `#metric-entry` (entry_jobs), `#metric-fresh` (posted_24h), `#metric-boards`
  (active_boards).
- **Search panel**
  - `#search-input` — free text (`q`), debounced 380 ms, `⌘K` / `Ctrl+K` focuses it.
  - **Experience chips** (`#level-chips`) — multi‑select `internship / entry / unknown /
    mid / senior`. Default selected: `internship, entry, unknown`.
  - **Posted‑within segmented control** (`#days-filter`) — `1 / 3 / 7 / 30 / any` days.
    Default `30`.
  - **Remote only** toggle (`#remote-filter`).
  - **Advanced filters** (collapsed) — company (`<select>` from `/api/filters`), location
    contains (free text + `<datalist>`), skill (`<select>`), source (ashby/greenhouse/
    lever), experience required (`max_experience`: 0/1/2/3/5 years minimum), employment
    type. Plus **Reset all filters**.
- **Results section** — result count, sort `<select>` (`entry / recent / experience /
  company`), an in‑progress sync progress bar (`#sync-progress`), the `#jobs-grid`, an
  empty state, and a **Load more** button.
- **Job modal** (`<dialog id="job-modal">`) — opened by clicking a card; fetches
  `/api/jobs/{id}` for the full description.
- **Toast** (`#toast`) — transient error messages.

## Client state (`app.js`)

```js
state = { page, pageSize: 24, total, levels: Set, days: "30", jobs: [], loading }
```

`buildParams()` serialises the current UI into the `/api/jobs` query string.
`activeFilterCount()` shows how many filters deviate from defaults on the Filters button.

### API calls

| Function | Endpoint | When |
|---|---|---|
| `loadJobs()` | `GET /api/jobs?<params>` | on load, and on any filter/sort change (resets to `page 1`) |
| `loadMore()` | `GET /api/jobs?<params>` with `page+1` | "Load more" click; appends |
| `openJob(id)` | `GET /api/jobs/{id}` | card click; fills the modal |
| `loadStats()` | `GET /api/stats` | on load, then every **60 s** |
| `loadFilters()` | `GET /api/filters` | on load only |
| `pollSync()` | `GET /api/sync-status` | on load, then every **15 s**; drives the status label + progress bar |

`init()` runs `bindEvents()` then `Promise.allSettled([loadStats, loadFilters, pollSync,
loadJobs])` and sets the two intervals.

### Rendering & safety

- Every interpolated value goes through `esc()` (HTML‑entity escape).
- `apply_url` is passed through `safeUrl()` — only `http:` / `https:` URLs are kept,
  everything else becomes `#`.
- `experienceLabel()` renders `Internship` / `min–max years` / `min+ years` /
  `Experience not specified` / `<level> level`.
- `salaryLabel()` renders `₹x–y LPA` for INR/year, otherwise a locale‑formatted range
  with period.
- `postedLabel()` → `Today` / `1 day ago` / `N days ago` from `days_posted`.
- Cards show a "New today" / "Fresh" badge when `days_posted <= 2`.
- Skeleton placeholders while loading; a dedicated empty state; a toast on fetch failure
  (the grid is hidden rather than showing stale data).

### Accessibility

`aria-busy` on the grid during loads, `aria-live="polite"` on the results section and
toast, `aria-expanded` on the advanced‑filters toggle, labelled controls, native
`<dialog>` for the modal with backdrop‑click and close‑button handling.

## Failure behaviour

- `/api/jobs` failure → grid hidden, toast shown, no crash.
- `/api/stats`, `/api/filters`, `/api/sync-status` failures are swallowed (the feed has
  its own visible error path); the sync label falls back to "Live company‑board feed".
- Opening a job that 404s (closed during a sync) shows "This role is no longer
  available." in the modal.
