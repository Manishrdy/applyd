/**
 * Dashboard — Alpine.js component for the unified search-first page.
 *
 * State kept entirely client-side:
 *   - filters (debounced changes trigger refetch + URL sync)
 *   - jobs (paginated, append-on-scroll)
 *   - facets (sidebar counts, refreshed on every filter change)
 *
 * The whole <body> is the Alpine root (see base.html `body_attrs` block),
 * so the header search input and the main grid share one scope.
 */

const POSTED_OPTIONS = [
  { value: 24,  label: "Last 24 hours" },
  { value: 48,  label: "Last 48 hours" },
  { value: 72,  label: "Last 72 hours" },
  { value: 168, label: "Last 7 days" },
  { value: 720, label: "Last 30 days" },
];

const SORT_OPTIONS = [
  { value: "newest",      label: "Newest first" },
  { value: "oldest",      label: "Oldest first" },
  { value: "salary_high", label: "Salary: high → low" },
  { value: "salary_low",  label: "Salary: low → high" },
  { value: "relevance",   label: "Relevance",         requires_q: true },
];

const DEFAULT_FILTERS = () => ({
  q: "",
  country: ["US"],
  ats: [],
  remote: null,            // null = any, true = remote, false = onsite
  employment_type: [],
  department: [],
  salary_min_usd: null,
  posted_hours: 24,
  include_undated: true,
});

function dashboard() {
  return {
    /* ---- exposed state ----------------------------------- */
    filters: DEFAULT_FILTERS(),
    sort: "newest",
    page: 1,
    limit: 50,

    jobs: [],
    total: 0,
    hasMore: false,
    isLoading: false,
    isLoadingMore: false,
    error: null,

    facets: { country: [], ats: [], employment_type: [], remote: [] },
    facetsTotal: 0,

    drawerOpen: false,

    /* ---- constants exposed to template ------------------- */
    POSTED_OPTIONS,
    SORT_OPTIONS,

    /* ---- lifecycle --------------------------------------- */
    async init() {
      this.loadFromUrl();
      // Load both in parallel — facets sidebar shouldn't block the result grid.
      await Promise.all([this.fetchJobs(true), this.fetchFacets()]);
      this.observeScrollSentinel();
    },

    /* ---- fetchers ---------------------------------------- */
    async fetchJobs(reset) {
      if (reset) {
        this.page = 1;
        this.jobs = [];
        this.isLoading = true;
      } else {
        this.isLoadingMore = true;
      }
      this.error = null;
      try {
        const url = `/api/jobs/?${this.buildParams({
          page: this.page,
          limit: this.limit,
          sort: this.effectiveSort(),
        })}`;
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this.jobs = reset ? data.jobs : [...this.jobs, ...data.jobs];
        this.total = data.total;
        this.hasMore = data.has_more;
      } catch (e) {
        this.error = e.message || "Failed to load jobs";
      } finally {
        this.isLoading = false;
        this.isLoadingMore = false;
      }
    },

    async fetchFacets() {
      try {
        const params = this.buildParams({
          facets: ["country", "ats", "employment_type", "remote"],
          limit_per_facet: 30,
        });
        const r = await fetch(`/api/jobs/facets?${params}`);
        if (!r.ok) return;
        const data = await r.json();
        const map = {};
        for (const g of data.facets) map[g.name] = g.counts;
        this.facets = { ...this.facets, ...map };
        this.facetsTotal = data.total_matching;
      } catch (e) {
        // Facets are non-critical — fail silently.
      }
    },

    async loadMore() {
      if (this.isLoading || this.isLoadingMore || !this.hasMore) return;
      this.page += 1;
      await this.fetchJobs(false);
    },

    /* ---- filter mutations -------------------------------- */
    onFilterChange() {
      this.syncUrl();
      this.fetchJobs(true);
      this.fetchFacets();
    },

    toggleArrayFilter(field, value) {
      const arr = this.filters[field];
      const i = arr.indexOf(value);
      if (i >= 0) arr.splice(i, 1);
      else arr.push(value);
      this.onFilterChange();
    },

    setRemote(value) {
      // value: 'any' | 'remote' | 'onsite'
      this.filters.remote = value === "any" ? null : value === "remote";
      this.onFilterChange();
    },

    clearAll() {
      const keep_q = this.filters.q;  // keep the search; clearAll clears filters only
      this.filters = DEFAULT_FILTERS();
      this.filters.q = keep_q;
      this.onFilterChange();
    },

    /* ---- chip dismissal ---------------------------------- */
    activeChips() {
      const chips = [];
      if (this.filters.q) {
        chips.push({ label: `"${this.filters.q}"`, key: "q" });
      }
      this.filters.country.forEach(c =>
        chips.push({ label: `country: ${c}`, key: "country", value: c }));
      this.filters.ats.forEach(a =>
        chips.push({ label: `ats: ${a}`, key: "ats", value: a }));
      this.filters.employment_type.forEach(e =>
        chips.push({ label: e, key: "employment_type", value: e }));
      if (this.filters.remote === true)  chips.push({ label: "remote only", key: "remote" });
      if (this.filters.remote === false) chips.push({ label: "on-site only", key: "remote" });
      if (this.filters.salary_min_usd) {
        chips.push({ label: `≥ $${this.filters.salary_min_usd.toLocaleString()}`, key: "salary_min_usd" });
      }
      if (!this.filters.include_undated) {
        chips.push({ label: "dated only", key: "include_undated" });
      }
      if (this.filters.posted_hours !== 24) {
        const opt = POSTED_OPTIONS.find(o => o.value === this.filters.posted_hours);
        if (opt) chips.push({ label: opt.label.toLowerCase(), key: "posted_hours" });
      }
      return chips;
    },

    removeChip(chip) {
      if (chip.key === "q") this.filters.q = "";
      else if (chip.key === "remote") this.filters.remote = null;
      else if (chip.key === "salary_min_usd") this.filters.salary_min_usd = null;
      else if (chip.key === "include_undated") this.filters.include_undated = true;
      else if (chip.key === "posted_hours") this.filters.posted_hours = 24;
      else if (Array.isArray(this.filters[chip.key])) {
        this.filters[chip.key] = this.filters[chip.key].filter(v => v !== chip.value);
      }
      this.onFilterChange();
    },

    /* ---- save toggle ------------------------------------- */
    async toggleSave(job) {
      const was = job.is_saved;
      job.is_saved = !was;        // optimistic
      try {
        const r = await fetch(`/api/saved/${job.id}`, {
          method: was ? "DELETE" : "POST",
        });
        if (!r.ok) throw new Error();
      } catch {
        job.is_saved = was;       // rollback
      }
    },

    /* ---- URL sync ---------------------------------------- */
    buildParams(extras = {}) {
      const p = new URLSearchParams();
      const f = this.filters;
      if (f.q) p.set("q", f.q);
      f.country.forEach(c => p.append("country", c));
      f.ats.forEach(a => p.append("ats", a));
      if (f.remote !== null) p.set("remote", f.remote);
      f.employment_type.forEach(e => p.append("employment_type", e));
      f.department.forEach(d => p.append("department", d));
      if (f.salary_min_usd) p.set("salary_min_usd", f.salary_min_usd);
      p.set("posted_hours", f.posted_hours);
      if (!f.include_undated) p.set("include_undated", "false");

      for (const [k, v] of Object.entries(extras)) {
        if (Array.isArray(v)) v.forEach(x => p.append(k, x));
        else if (v != null) p.set(k, v);
      }
      return p.toString();
    },

    syncUrl() {
      const params = this.buildParams({ sort: this.sort });
      const url = `${window.location.pathname}?${params}`;
      window.history.replaceState({}, "", url);
    },

    loadFromUrl() {
      const p = new URLSearchParams(window.location.search);
      const f = this.filters;
      if (p.has("q")) f.q = p.get("q");
      if (p.has("country")) f.country = p.getAll("country");
      if (p.has("ats")) f.ats = p.getAll("ats");
      if (p.has("remote")) f.remote = p.get("remote") === "true";
      if (p.has("employment_type")) f.employment_type = p.getAll("employment_type");
      if (p.has("department")) f.department = p.getAll("department");
      if (p.has("salary_min_usd")) f.salary_min_usd = parseInt(p.get("salary_min_usd"), 10) || null;
      if (p.has("posted_hours")) f.posted_hours = parseInt(p.get("posted_hours"), 10) || 24;
      if (p.has("include_undated")) f.include_undated = p.get("include_undated") !== "false";
      if (p.has("sort")) this.sort = p.get("sort");
    },

    /* ---- sort effective handling ------------------------- */
    effectiveSort() {
      // relevance is only meaningful when q is set
      if (this.sort === "relevance" && !this.filters.q) return "newest";
      return this.sort;
    },

    /* ---- infinite scroll --------------------------------- */
    observeScrollSentinel() {
      const sentinel = this.$refs.sentinel;
      if (!sentinel) return;
      const obs = new IntersectionObserver(entries => {
        if (entries[0].isIntersecting) this.loadMore();
      }, { rootMargin: "600px" });
      obs.observe(sentinel);
    },

    /* ---- formatters used in template --------------------- */
    formatSalary(job) {
      const min = job.salary_min_usd_annual;
      const max = job.salary_max_usd_annual;
      if (min == null && max == null) {
        return job.salary_summary || "—";
      }
      const fmt = v => `$${Math.round(v / 1000)}k`;
      if (min && max && min !== max) return `${fmt(min)}–${fmt(max)}`;
      return fmt(max || min);
    },

    formatTimeAgo(iso) {
      if (!iso) return "—";
      const ts = new Date(iso);
      if (isNaN(ts.getTime())) return "—";
      const secs = Math.floor((Date.now() - ts.getTime()) / 1000);
      if (secs < 60)     return `${secs}s ago`;
      if (secs < 3600)   return `${Math.floor(secs / 60)}m ago`;
      if (secs < 86400)  return `${Math.floor(secs / 3600)}h ago`;
      const days = Math.floor(secs / 86400);
      if (days < 30)     return `${days}d ago`;
      return `${Math.floor(days / 30)}mo ago`;
    },

    formatTimeLabel(job) {
      // dated rows say "Posted Xh ago", undated say "First seen Xh ago"
      const date = job.posted_at || job.first_seen_at;
      const verb = job.is_dated ? "Posted" : "First seen";
      return `${verb} ${this.formatTimeAgo(date)}`;
    },

    formatLocation(job) {
      const loc = job.location || "";
      if (job.is_remote) return loc ? `${loc} · Remote` : "Remote";
      return loc || "—";
    },

    facetLabel(name, value) {
      if (name === "remote") return value ? "Remote" : "On-site";
      if (value === null || value === "") return "—";
      return value;
    },

    /* ---- multi-select helpers --------------------------- */
    isChecked(field, value) {
      return this.filters[field].includes(value);
    },
  };
}

// Expose globally so Alpine x-data="dashboard()" finds it.
window.dashboard = dashboard;
