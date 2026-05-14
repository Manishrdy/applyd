/**
 * /settings page — Alpine component.
 *
 * Read-mostly observability + a manual "Refresh now" trigger that calls
 * /api/ingest. Polls /api/ingest/status while a refresh is running.
 */

function settingsPage() {
  return {
    info: null,
    byAts: [],
    log: [],
    isLoading: true,
    isRefreshing: false,
    refreshResult: null,
    error: null,

    async init() {
      await this.fetchAll();
    },

    async fetchAll() {
      this.isLoading = true;
      try {
        const [info, byAts, log] = await Promise.all([
          fetch("/api/settings/").then(r => r.json()),
          fetch("/api/settings/by_ats").then(r => r.json()),
          fetch("/api/settings/ingest_log?limit=20").then(r => r.json()),
        ]);
        this.info = info;
        this.byAts = byAts;
        this.log = log;
        this.error = null;
      } catch (e) {
        this.error = e.message || "Failed to load settings";
      } finally {
        this.isLoading = false;
      }
    },

    async refreshNow(force = false) {
      if (this.isRefreshing) return;
      this.isRefreshing = true;
      this.refreshResult = null;
      try {
        const r = await fetch(`/api/ingest?force=${force}`, { method: "POST" });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
        this.refreshResult = data;
        await this.fetchAll();
      } catch (e) {
        this.refreshResult = { status: "failed", error: e.message };
      } finally {
        this.isRefreshing = false;
      }
    },

    fmtBytes(n) {
      if (n == null) return "—";
      const units = ["B", "KB", "MB", "GB", "TB"];
      let i = 0;
      while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
      return `${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${units[i]}`;
    },

    fmtNum(n) {
      return (n ?? 0).toLocaleString();
    },

    fmtDuration(s) {
      if (s == null) return "—";
      if (s < 60) return `${s.toFixed(1)}s`;
      const m = Math.floor(s / 60);
      return `${m}m ${Math.round(s - m * 60)}s`;
    },

    fmtTime(iso) {
      // SQLite emits "YYYY-MM-DD HH:MM:SS" (UTC, no TZ marker). Normalize to
      // T-separated + Z so JS parses as UTC then renders in local time.
      if (!iso) return "—";
      let s = String(iso).replace(" ", "T");
      if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
      const d = new Date(s);
      if (isNaN(d.getTime())) return iso;
      return d.toLocaleString();
    },

    statusVariant(s) {
      return { success: "success", failed: "danger", skipped: "neutral" }[s] || "neutral";
    },
  };
}

window.settingsPage = settingsPage;
