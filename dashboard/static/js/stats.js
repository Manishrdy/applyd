/**
 * /stats page — Alpine + Chart.js.
 *
 * Charts pull colors from CSS variables via getComputedStyle, so theme
 * toggles re-paint the charts via a MutationObserver on <html>'s class.
 *
 * Country selector re-runs the country-scoped queries (donut, line,
 * histogram, top companies). The global ones (by ATS, by country, summary)
 * stay constant.
 */

const REMOTE_LABELS = ["Remote", "On-site", "Unknown"];

function statsPage() {
  return {
    /* ---- state ----------------------------------------- */
    country: "US",
    isLoading: true,
    error: null,

    summary: null,
    byAts: [],
    byCountry: [],
    topCompanies: [],
    salaryRange: null,
    remoteVsOnsite: null,
    byDay: [],

    charts: {},   // { byAts, remote, byDay, salary } — Chart.js instances

    /* ---- lifecycle ------------------------------------- */
    async init() {
      const p = new URLSearchParams(window.location.search);
      if (p.has("country")) this.country = p.get("country");

      await this.fetchAll();
      this.$nextTick(() => {
        this.renderCharts();
        this.observeThemeChanges();
      });
    },

    /* ---- data fetchers --------------------------------- */
    async fetchAll() {
      this.isLoading = true;
      this.error = null;
      try {
        const c = encodeURIComponent(this.country);
        const [summary, byAts, byCountry, topCompanies, salaryRange, remote, byDay] = await Promise.all([
          fetch("/api/stats/summary").then(r => r.json()),
          fetch("/api/stats/by_ats?days=30&limit=15").then(r => r.json()),
          fetch("/api/stats/by_country?days=30&limit=20").then(r => r.json()),
          fetch(`/api/stats/top_companies?days=7&country=${c}&limit=20`).then(r => r.json()),
          fetch(`/api/stats/salary_range?country=${c}&days=30`).then(r => r.json()),
          fetch(`/api/stats/remote_vs_onsite?country=${c}&days=30`).then(r => r.json()),
          fetch(`/api/stats/by_day?country=${c}&days=30`).then(r => r.json()),
        ]);
        this.summary = summary;
        this.byAts = byAts.items;
        this.byCountry = byCountry.items;
        this.topCompanies = topCompanies.items;
        this.salaryRange = salaryRange;
        this.remoteVsOnsite = remote;
        this.byDay = byDay.items;
      } catch (e) {
        this.error = e.message || "Failed to load stats";
      } finally {
        this.isLoading = false;
      }
    },

    async setCountry(value) {
      if (value === this.country) return;
      this.country = value;
      const url = `/stats?country=${encodeURIComponent(value)}`;
      window.history.replaceState({}, "", url);
      await this.fetchAll();
      // Country-scoped charts need re-init with new data
      this.destroyChart("remote");
      this.destroyChart("byDay");
      this.destroyChart("salary");
      this.$nextTick(() => {
        this.renderRemote();
        this.renderByDay();
        this.renderSalary();
      });
    },

    /* ---- theme awareness -------------------------------- */
    themeColors() {
      const styles = getComputedStyle(document.documentElement);
      const v = (name) => styles.getPropertyValue(name).trim();
      const hsl = (name, alpha) =>
        alpha == null ? `hsl(${v(name)})` : `hsl(${v(name)} / ${alpha})`;
      return {
        accent:      hsl("--accent"),
        accentLow:   hsl("--accent", 0.15),
        accentMed:   hsl("--accent", 0.5),
        text:        hsl("--text"),
        textMuted:   hsl("--text-muted"),
        textSubtle:  hsl("--text-subtle"),
        border:      hsl("--border"),
        surface:     hsl("--surface"),
        success:     hsl("--success"),
        warning:     hsl("--warning"),
        danger:      hsl("--danger"),
        info:        hsl("--info"),
      };
    },

    observeThemeChanges() {
      new MutationObserver(() => {
        // small delay so the CSS vars in :root.dark have settled
        setTimeout(() => this.repaintCharts(), 30);
      }).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["class"],
      });
    },

    repaintCharts() {
      const colors = this.themeColors();
      for (const chart of Object.values(this.charts)) {
        if (!chart) continue;
        applyTheme(chart, colors);
        chart.update("none");
      }
    },

    destroyChart(key) {
      if (this.charts[key]) {
        this.charts[key].destroy();
        this.charts[key] = null;
      }
    },

    /* ---- chart factories -------------------------------- */
    renderCharts() {
      this.renderByAts();
      this.renderRemote();
      this.renderByDay();
      this.renderSalary();
    },

    renderByAts() {
      const ctx = document.getElementById("chart-by-ats");
      if (!ctx || !this.byAts.length) return;
      const colors = this.themeColors();
      this.charts.byAts = new Chart(ctx, {
        type: "bar",
        data: {
          labels: this.byAts.map(r => r.label),
          datasets: [{
            data: this.byAts.map(r => r.count),
            backgroundColor: colors.accent,
            borderRadius: 4,
            barPercentage: 0.85,
          }],
        },
        options: chartBaseOptions(colors, {
          indexAxis: "y",
          plugins: { legend: { display: false } },
          scales: {
            x: axis(colors, { beginAtZero: true }),
            y: axis(colors, { ticks: { autoSkip: false } }),
          },
        }),
      });
    },

    renderRemote() {
      const ctx = document.getElementById("chart-remote");
      const r = this.remoteVsOnsite;
      if (!ctx || !r) return;
      const colors = this.themeColors();
      const data = [r.remote, r.onsite, r.unknown];
      this.charts.remote = new Chart(ctx, {
        type: "doughnut",
        data: {
          labels: REMOTE_LABELS,
          datasets: [{
            data,
            backgroundColor: [colors.success, colors.accent, colors.textSubtle],
            borderColor: colors.surface,
            borderWidth: 2,
          }],
        },
        options: chartBaseOptions(colors, {
          cutout: "65%",
          plugins: {
            legend: { position: "bottom", labels: { color: colors.text, padding: 16, boxWidth: 12 } },
            tooltip: tooltipOptions(colors),
          },
        }),
      });
    },

    renderByDay() {
      const ctx = document.getElementById("chart-by-day");
      if (!ctx || !this.byDay.length) return;
      const colors = this.themeColors();
      this.charts.byDay = new Chart(ctx, {
        type: "line",
        data: {
          labels: this.byDay.map(r => r.label),
          datasets: [{
            data: this.byDay.map(r => r.count),
            borderColor: colors.accent,
            backgroundColor: colors.accentLow,
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            pointHoverRadius: 4,
            borderWidth: 2,
          }],
        },
        options: chartBaseOptions(colors, {
          plugins: {
            legend: { display: false },
            tooltip: tooltipOptions(colors),
          },
          scales: {
            x: axis(colors, { ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 8 } }),
            y: axis(colors, { beginAtZero: true }),
          },
        }),
      });
    },

    renderSalary() {
      const ctx = document.getElementById("chart-salary");
      const sr = this.salaryRange;
      if (!ctx || !sr || !sr.buckets) return;
      const colors = this.themeColors();
      const labels = sr.buckets.map(b => b.high == null
        ? `$${formatK(b.low)}+`
        : `$${formatK(b.low)}–${formatK(b.high)}`);
      this.charts.salary = new Chart(ctx, {
        type: "bar",
        data: {
          labels,
          datasets: [{
            data: sr.buckets.map(b => b.count),
            backgroundColor: colors.accent,
            borderRadius: 4,
            barPercentage: 0.85,
          }],
        },
        options: chartBaseOptions(colors, {
          plugins: {
            legend: { display: false },
            tooltip: tooltipOptions(colors),
          },
          scales: {
            x: axis(colors, { ticks: { color: colors.textMuted } }),
            y: axis(colors, { beginAtZero: true }),
          },
        }),
      });
    },

    /* ---- helpers used by template ---------------------- */
    fmtNum(n) {
      return (n || 0).toLocaleString();
    },
    fmtSalary(v) {
      if (v == null) return "—";
      return `$${formatK(v)}`;
    },
  };
}

/* ---- shared chart helpers ---------------------------- */

function formatK(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${Math.round(n / 1_000)}k`;
  return `${n}`;
}

function axis(colors, extra = {}) {
  return {
    ...extra,
    ticks: { color: colors.textMuted, ...(extra.ticks || {}) },
    grid: { color: colors.border, drawBorder: false, ...(extra.grid || {}) },
    border: { color: colors.border },
  };
}

function tooltipOptions(colors) {
  return {
    backgroundColor: colors.surface,
    borderColor: colors.border,
    borderWidth: 1,
    titleColor: colors.text,
    bodyColor: colors.text,
    padding: 10,
    displayColors: true,
    boxWidth: 8,
    boxHeight: 8,
    cornerRadius: 8,
  };
}

function chartBaseOptions(colors, extra = {}) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { labels: { color: colors.text } },
      tooltip: tooltipOptions(colors),
      ...(extra.plugins || {}),
    },
    ...Object.fromEntries(Object.entries(extra).filter(([k]) => k !== "plugins")),
  };
}

function applyTheme(chart, colors) {
  // Re-paint axes
  if (chart.options.scales) {
    for (const key of Object.keys(chart.options.scales)) {
      const s = chart.options.scales[key];
      if (s.ticks) s.ticks.color = colors.textMuted;
      if (s.grid) s.grid.color = colors.border;
      if (s.border) s.border.color = colors.border;
    }
  }
  if (chart.options.plugins) {
    if (chart.options.plugins.legend?.labels) {
      chart.options.plugins.legend.labels.color = colors.text;
    }
    if (chart.options.plugins.tooltip) {
      Object.assign(chart.options.plugins.tooltip, tooltipOptions(colors));
    }
  }
  // Re-paint datasets
  for (const ds of chart.data.datasets) {
    if (chart.config.type === "doughnut" || chart.config.type === "pie") {
      ds.backgroundColor = [colors.success, colors.accent, colors.textSubtle];
      ds.borderColor = colors.surface;
    } else if (chart.config.type === "line") {
      ds.borderColor = colors.accent;
      ds.backgroundColor = colors.accentLow;
    } else {
      ds.backgroundColor = colors.accent;
    }
  }
}

window.statsPage = statsPage;
