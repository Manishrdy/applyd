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

// Curated role taxonomy — keys must match app/services/roles.py::ROLES.
const ROLE_OPTIONS = [
  { value: "software_engineer",          label: "Software Engineer" },
  { value: "senior_software_engineer",   label: "Senior Software Engineer" },
  { value: "staff_plus_engineer",        label: "Staff+ Engineer" },
  { value: "backend_engineer",           label: "Backend Engineer" },
  { value: "frontend_engineer",          label: "Frontend Engineer" },
  { value: "fullstack_engineer",         label: "Full-stack Engineer" },
  { value: "ai_ml_engineer",             label: "AI / ML Engineer" },
  { value: "forward_deployed_engineer",  label: "Forward Deployed Engineer" },
  { value: "founding_engineer",          label: "Founding Engineer" },
  { value: "product_engineer",           label: "Product Engineer" },
  { value: "mobile_engineer",            label: "Mobile Engineer" },
  { value: "data_engineer",              label: "Data Engineer" },
  { value: "sre_platform_engineer",      label: "SRE / Platform" },
  { value: "security_engineer",          label: "Security Engineer" },
];

const SENIORITY_OPTIONS = [
  { value: "junior",    label: "Junior" },
  { value: "mid",       label: "Mid" },
  { value: "senior",    label: "Senior" },
  { value: "staff",     label: "Staff" },
  { value: "principal", label: "Principal+" },
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
  roles: [],
  seniority: [],
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
    ROLE_OPTIONS,
    SENIORITY_OPTIONS,

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
        chips.push({ label: `ats: ${this.facetLabel("ats", a)}`, key: "ats", value: a }));
      this.filters.employment_type.forEach(e =>
        chips.push({ label: this.facetLabel("employment_type", e), key: "employment_type", value: e }));
      (this.filters.roles || []).forEach(r => {
        const opt = ROLE_OPTIONS.find(o => o.value === r);
        chips.push({ label: opt ? opt.label : r, key: "roles", value: r });
      });
      (this.filters.seniority || []).forEach(s => {
        const opt = SENIORITY_OPTIONS.find(o => o.value === s);
        chips.push({ label: opt ? opt.label : s, key: "seniority", value: s });
      });
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
        // Notify the header counter (savedCounter listens for this)
        window.dispatchEvent(new CustomEvent("applyd:saved-changed", {
          detail: { delta: was ? -1 : +1 },
        }));
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
      (f.roles || []).forEach(r => p.append("role", r));
      (f.seniority || []).forEach(s => p.append("seniority", s));
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
      if (p.has("role")) f.roles = p.getAll("role");
      if (p.has("seniority")) f.seniority = p.getAll("seniority");
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

    parseUtcIso(iso) {
      // Server returns SQLite-format "YYYY-MM-DD HH:MM:SS" (no TZ marker, UTC)
      // and ISO-like "YYYY-MM-DDTHH:MM:SS" from upstream. Both lack a timezone
      // indicator, so naive `new Date(iso)` parses them as LOCAL time — which
      // shifts a UTC timestamp by the local offset and produces nonsense like
      // "-335s ago" for a row first seen seconds ago. Normalize to T-separated
      // with a trailing Z so JS parses as UTC.
      if (!iso) return null;
      let s = String(iso).replace(" ", "T");
      if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
      const d = new Date(s);
      return isNaN(d.getTime()) ? null : d;
    },

    formatTimeAgo(iso) {
      const ts = this.parseUtcIso(iso);
      if (!ts) return "—";
      // Clamp tiny negative skew (clock jitter between server and client) to 0
      // so we never show "-3s ago".
      const secs = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
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

    isFresh(job) {
      // True when effective date is within the last 24h. Drives the small
      // accent dot before the card title.
      const date = job.posted_at || job.first_seen_at;
      const ts = this.parseUtcIso(date);
      if (!ts) return false;
      return Date.now() - ts.getTime() < 24 * 3600 * 1000;
    },

    cardPrimaryMeta(job) {
      // Salary → department → fallback. Returns text + a "kind" so the
      // template can switch typography (mono+strong for salary, muted
      // sans for department/fallback).
      const min = job.salary_min_usd_annual;
      const max = job.salary_max_usd_annual;
      if (min != null || max != null) {
        return { text: this.formatSalary(job), kind: "salary" };
      }
      if (job.salary_summary) {
        return { text: job.salary_summary, kind: "salary" };
      }
      if (job.department) {
        return { text: job.department, kind: "department" };
      }
      return { text: "Salary not disclosed", kind: "muted" };
    },

    facetLabel(name, value) {
      const COUNTRY_LABELS = {
        US: "United States",
        IN: "India",
        EU: "European Union",
        GB: "United Kingdom",
        CA: "Canada",
        CH: "Switzerland",
        IL: "Israel",
      };
      if (name === "remote") return value ? "Remote" : "On-site";
      if (name === "country") {
        if (value === null || value === "") return "Rest of World";
        return COUNTRY_LABELS[value] || value;
      }
      if (name === "employment_type") {
        // Unknowns are visually merged into Fulltime (see employmentFacetRows).
        if (value === null || value === "") return "Fulltime";
        const cleaned = String(value).replace(/_/g, "").toLowerCase();
        return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
      }
      if (name === "ats") {
        if (value === null || value === "") return "—";
        const cleaned = String(value).replace(/_/g, "").toLowerCase();
        return cleaned.charAt(0).toUpperCase() + cleaned.slice(1);
      }
      if (value === null || value === "") return "—";
      return value;
    },

    countryFacetRows() {
      const priority = ["US", "IN", "EU", "GB", "CA", "CH", "IL"];
      const byCode = new Map((this.facets.country || []).map(r => [r.value, r]));
      const pinned = priority.map(code => byCode.get(code) || { value: code, count: 0 });
      const rest = (this.facets.country || []).filter(r => !priority.includes(r.value));
      return [...pinned, ...rest].slice(0, 20);
    },

    employmentFacetRows() {
      // Fold the null/empty "unknown" row into FULL_TIME so the sidebar
      // doesn't show a separate "—" bucket. Display-only: filter behavior
      // is unchanged (clicking Fulltime still filters server-side by
      // employment_type=FULL_TIME).
      const rows = this.facets.employment_type || [];
      const unknownCount = rows
        .filter(r => r.value === null || r.value === "")
        .reduce((s, r) => s + r.count, 0);
      const visible = rows.filter(r => r.value !== null && r.value !== "");
      if (unknownCount > 0) {
        const ft = visible.find(r => r.value === "FULL_TIME");
        if (ft) {
          ft.count += unknownCount;
        } else {
          visible.unshift({ value: "FULL_TIME", count: unknownCount });
        }
      }
      return visible.slice(0, 8);
    },

    /* ---- multi-select helpers --------------------------- */
    isChecked(field, value) {
      return this.filters[field].includes(value);
    },
  };
}

// Expose globally so Alpine x-data="dashboard()" finds it.
window.dashboard = dashboard;
