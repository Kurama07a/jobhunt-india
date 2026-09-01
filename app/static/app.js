const state = {
  page: 1,
  pageSize: 24,
  total: 0,
  levels: new Set(["internship", "entry", "unknown"]),
  days: "30",
  jobs: [],
  loading: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const formatter = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
let searchTimer;
let toastTimer;

function esc(value = "") {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
  } catch {
    return "#";
  }
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 3500);
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Request failed (${response.status})`);
  return response.json();
}

function initials(company) {
  const words = String(company || "Job").trim().split(/\s+/);
  return words.slice(0, 2).map((word) => word[0]).join("");
}

function postedLabel(days) {
  if (days === 0) return "Today";
  if (days === 1) return "1 day ago";
  return `${days} days ago`;
}

function experienceLabel(job) {
  if (job.experience_level === "internship") return "Internship";
  if (job.experience_min !== null && job.experience_min !== undefined) {
    const min = Number(job.experience_min);
    const max = job.experience_max === null || job.experience_max === undefined ? null : Number(job.experience_max);
    if (max !== null) return `${min}–${max} years`;
    return `${min}+ years`;
  }
  return job.experience_level === "unknown" ? "Experience not specified" : `${job.experience_level} level`;
}

function levelLabel(level) {
  return ({ internship: "Internship", entry: "Entry friendly", mid: "Mid level", senior: "Senior", unknown: "Open level" })[level] || level;
}

function salaryLabel(job) {
  if (job.salary_min === null || job.salary_max === null) return "";
  const currency = job.salary_currency === "INR" ? "₹" : `${job.salary_currency || ""} `;
  if (job.salary_currency === "INR" && job.salary_period === "year") {
    return `${currency}${Number(job.salary_min) / 100000}–${Number(job.salary_max) / 100000} LPA`;
  }
  return `${currency}${formatter.format(job.salary_min)}–${formatter.format(job.salary_max)} / ${job.salary_period || "year"}`;
}

function buildParams() {
  const params = new URLSearchParams({
    page: String(state.page),
    page_size: String(state.pageSize),
    sort: $("#sort-filter").value,
  });
  const search = $("#search-input").value.trim();
  if (search) params.set("q", search);
  if (state.levels.size) params.set("levels", [...state.levels].join(","));
  if (state.days) params.set("days", state.days);
  if ($("#remote-filter").checked) params.set("remote", "true");
  const mappings = [
    ["#company-filter", "company"],
    ["#location-filter", "location"],
    ["#skill-filter", "skills"],
    ["#ats-filter", "ats"],
    ["#experience-filter", "max_experience"],
    ["#employment-filter", "employment_type"],
  ];
  mappings.forEach(([selector, name]) => {
    const value = $(selector).value.trim();
    if (value) params.set(name, value);
  });
  return params;
}

function activeFilterCount() {
  let count = 0;
  if ($("#search-input").value.trim()) count += 1;
  if (state.days) count += 1;
  if ($("#remote-filter").checked) count += 1;
  if (state.levels.size !== 3 || !["internship", "entry", "unknown"].every((level) => state.levels.has(level))) count += 1;
  ["#company-filter", "#location-filter", "#skill-filter", "#ats-filter", "#experience-filter", "#employment-filter"].forEach((selector) => {
    if ($(selector).value.trim()) count += 1;
  });
  $("#filter-count").textContent = count;
}

function skeletons() {
  $("#jobs-grid").innerHTML = Array.from({ length: 9 }, () => '<div class="skeleton"></div>').join("");
  $("#jobs-grid").setAttribute("aria-busy", "true");
}

function jobCard(job) {
  const skills = (job.skills || []).slice(0, 5).map((skill) => `<span class="skill-tag">${esc(skill)}</span>`).join("");
  const fresh = job.days_posted <= 2 ? `<span class="fresh-badge">${job.days_posted === 0 ? "New today" : "Fresh"}</span>` : "";
  const location = job.location || (job.is_remote ? "Remote" : "Location not stated");
  const salary = salaryLabel(job);
  return `
    <article class="job-card" data-job-id="${esc(job.id)}">
      <div class="job-top">
        <div class="company-lockup">
          <span class="company-logo">${esc(initials(job.company))}</span>
          <span class="company-name">${esc(job.company)}<small class="source-name">via ${esc(job.ats)}</small></span>
        </div>
        ${fresh}
      </div>
      <h3>${esc(job.title)}</h3>
      <div class="job-meta">
        <span><i class="meta-dot"></i>${esc(location)}</span>
        <span>${esc(postedLabel(job.days_posted))}</span>
        ${job.is_remote ? "<span>Remote</span>" : ""}
        ${salary ? `<span>${esc(salary)}</span>` : ""}
      </div>
      <div class="skills-row">${skills || '<span class="skill-tag">Software engineering</span>'}</div>
      <div class="job-bottom">
        <div class="level-wrap">
          <span class="level-badge">${esc(levelLabel(job.experience_level))}</span>
          <small class="experience-copy">${esc(experienceLabel(job))}</small>
        </div>
        <a class="apply-link" href="${esc(safeUrl(job.apply_url))}" target="_blank" rel="noopener noreferrer" aria-label="Apply to ${esc(job.title)}">↗</a>
      </div>
    </article>`;
}

function renderJobs(append = false) {
  const grid = $("#jobs-grid");
  const html = state.jobs.map(jobCard).join("");
  if (append) grid.insertAdjacentHTML("beforeend", html);
  else grid.innerHTML = html;
  grid.setAttribute("aria-busy", "false");
}

async function loadJobs({ append = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  $("#empty-state").hidden = true;
  $("#load-more").disabled = true;
  if (!append) skeletons();
  activeFilterCount();
  try {
    const data = await api(`/api/jobs?${buildParams()}`);
    state.total = data.pagination.total;
    state.jobs = data.jobs;
    renderJobs(append);
    $("#result-count").textContent = formatter.format(state.total);
    $("#empty-state").hidden = state.total !== 0;
    $("#jobs-grid").hidden = state.total === 0;
    $("#load-more").hidden = data.pagination.page >= data.pagination.pages;
  } catch (error) {
    $("#jobs-grid").innerHTML = "";
    $("#jobs-grid").hidden = true;
    showToast("The job feed could not be loaded. Retrying shortly may help.");
  } finally {
    state.loading = false;
    $("#load-more").disabled = false;
  }
}

async function loadMore() {
  state.page += 1;
  const prior = state.jobs;
  try {
    state.loading = false;
    const params = buildParams();
    $("#load-more").disabled = true;
    const data = await api(`/api/jobs?${params}`);
    const nextJobs = data.jobs;
    state.jobs = nextJobs;
    renderJobs(true);
    state.jobs = [...prior, ...nextJobs];
    $("#load-more").hidden = data.pagination.page >= data.pagination.pages;
  } catch {
    state.page -= 1;
    showToast("Could not load the next page.");
  } finally {
    $("#load-more").disabled = false;
  }
}

async function openJob(id) {
  const modal = $("#job-modal");
  $("#modal-content").innerHTML = '<div class="modal-body"><div class="skeleton"></div></div>';
  modal.showModal();
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(id)}`);
    const facts = [
      job.location || "Location not stated",
      job.is_remote ? "Remote" : "On-site / hybrid",
      experienceLabel(job),
      job.employment_type,
      ...(job.skills || []).slice(0, 7),
    ].filter(Boolean).map((fact) => `<span>${esc(fact)}</span>`).join("");
    $("#modal-content").innerHTML = `
      <div class="modal-body">
        <div class="modal-eyebrow">${esc(levelLabel(job.experience_level))} · ${esc(job.ats)}</div>
        <h2>${esc(job.title)}</h2>
        <div class="modal-company">${esc(job.company)}</div>
        <div class="modal-facts">${facts}</div>
        <div class="modal-description"></div>
        <a class="modal-apply" href="${esc(safeUrl(job.apply_url))}" target="_blank" rel="noopener noreferrer">Open the official application ↗</a>
      </div>`;
    $(".modal-description").textContent = job.description || "The company did not publish a description through its public job-board API. Open the official application for full details.";
  } catch {
    $("#modal-content").innerHTML = '<div class="modal-body"><h2>This role is no longer available.</h2><p class="modal-company">It may have closed during the latest sync.</p></div>';
  }
}

async function loadStats() {
  try {
    const data = await api("/api/stats");
    $("#metric-jobs").textContent = formatter.format(data.active_jobs || 0);
    $("#metric-entry").textContent = formatter.format(data.entry_jobs || 0);
    $("#metric-fresh").textContent = formatter.format(data.posted_24h || 0);
    $("#metric-boards").textContent = formatter.format(data.active_boards || 0);
  } catch {
    // The job list has its own user-facing failure state.
  }
}

async function loadFilters() {
  try {
    const data = await api("/api/filters");
    $("#company-filter").insertAdjacentHTML("beforeend", data.companies.map((item) => `<option value="${esc(item.value)}">${esc(item.value)} (${formatter.format(item.count)})</option>`).join(""));
    $("#skill-filter").insertAdjacentHTML("beforeend", data.skills.map((item) => `<option value="${esc(item.value)}">${esc(item.value)} (${formatter.format(item.count)})</option>`).join(""));
    $("#locations-list").innerHTML = data.locations.map((item) => `<option value="${esc(item.value)}"></option>`).join("");
  } catch {
    // Advanced filters remain usable as free text where applicable.
  }
}

function syncLabel(run) {
  if (!run || run.status === "never_run") return "Waiting for the first live sync";
  if (["running", "queued"].includes(run.status)) return "Live refresh in progress";
  if (!run.finished_at) return "Live company-board feed";
  const finished = new Date(run.finished_at);
  const minutes = Math.max(0, Math.round((Date.now() - finished.getTime()) / 60000));
  if (minutes < 2) return "Updated just now";
  if (minutes < 60) return `Updated ${minutes}m ago`;
  return `Updated ${Math.round(minutes / 60)}h ago`;
}

async function pollSync() {
  try {
    const run = await api("/api/sync-status");
    $("#sync-label").textContent = syncLabel(run);
    const running = ["running", "queued"].includes(run.status);
    $("#sync-progress").hidden = !running;
    if (running) {
      const total = Number(run.boards_total || 0);
      const checked = Number(run.boards_checked || 0);
      const percent = total ? Math.max(2, Math.min(100, (checked / total) * 100)) : 3;
      $("#sync-progress-bar").style.width = `${percent}%`;
      $("#sync-progress-text").textContent = total ? `${formatter.format(checked)} of ${formatter.format(total)} boards checked` : "Discovering company boards…";
    }
  } catch {
    $("#sync-label").textContent = "Live company-board feed";
  }
}

function resetFilters() {
  state.levels = new Set(["internship", "entry", "unknown"]);
  state.days = "30";
  $$("#level-chips .chip").forEach((button) => button.classList.toggle("active", state.levels.has(button.dataset.level)));
  $$("#days-filter button").forEach((button) => button.classList.toggle("active", button.dataset.days === "30"));
  $("#search-input").value = "";
  $("#remote-filter").checked = false;
  ["#company-filter", "#location-filter", "#skill-filter", "#ats-filter", "#experience-filter", "#employment-filter"].forEach((selector) => { $(selector).value = ""; });
  $("#sort-filter").value = "entry";
  state.page = 1;
  loadJobs();
}

function bindEvents() {
  $("#jobs-grid").addEventListener("click", (event) => {
    if (event.target.closest("a")) return;
    const card = event.target.closest(".job-card");
    if (card) openJob(card.dataset.jobId);
  });
  $("#advanced-toggle").addEventListener("click", () => {
    const filters = $("#advanced-filters");
    filters.hidden = !filters.hidden;
    $("#advanced-toggle").setAttribute("aria-expanded", String(!filters.hidden));
  });
  $$("#level-chips .chip").forEach((button) => {
    button.addEventListener("click", () => {
      const level = button.dataset.level;
      if (state.levels.has(level)) state.levels.delete(level);
      else state.levels.add(level);
      button.classList.toggle("active");
      state.page = 1;
      loadJobs();
    });
  });
  $$("#days-filter button").forEach((button) => {
    button.addEventListener("click", () => {
      state.days = button.dataset.days;
      $$("#days-filter button").forEach((candidate) => candidate.classList.toggle("active", candidate === button));
      state.page = 1;
      loadJobs();
    });
  });
  $("#search-input").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.page = 1; loadJobs(); }, 380);
  });
  ["#remote-filter", "#company-filter", "#skill-filter", "#ats-filter", "#experience-filter", "#employment-filter", "#sort-filter"].forEach((selector) => {
    $(selector).addEventListener("change", () => { state.page = 1; loadJobs(); });
  });
  $("#location-filter").addEventListener("change", () => { state.page = 1; loadJobs(); });
  $("#clear-filters").addEventListener("click", resetFilters);
  $("#empty-reset").addEventListener("click", resetFilters);
  $("#load-more").addEventListener("click", loadMore);
  $("#modal-close").addEventListener("click", () => $("#job-modal").close());
  $("#job-modal").addEventListener("click", (event) => {
    if (event.target === $("#job-modal")) $("#job-modal").close();
  });
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      $("#search-input").focus();
    }
  });
}

async function init() {
  bindEvents();
  await Promise.allSettled([loadStats(), loadFilters(), pollSync(), loadJobs()]);
  setInterval(pollSync, 15000);
  setInterval(loadStats, 60000);
}

init();
