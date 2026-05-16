/**
 * myActivity() — Alpine factory for the "My activity" strip on /dashboard.
 *
 * Fetches /api/me/stats once on init, re-fetches on `applyd:saved-changed`
 * so tiles stay in sync after toggling a bookmark. No chart library — the
 * 30-day trend is a hand-rolled SVG polyline (see sparkline()).
 */
function myActivity() {
  return {
    loaded: false,
    error: null,
    total_saved: 0,
    by_status: { queued: 0, applied: 0, skipped: 0, archived: 0 },
    saves_per_day: [],
    top_companies: [],
    top_ats: [],
    conversion_rate: 0,

    async init() {
      await this.refresh();
      this.loaded = true;
      window.addEventListener("applyd:saved-changed", () => this.refresh());
    },

    async refresh() {
      try {
        const r = await fetch("/api/me/stats");
        if (!r.ok) {
          this.error = `HTTP ${r.status}`;
          return;
        }
        Object.assign(this, await r.json());
        this.error = null;
      } catch (e) {
        this.error = e?.message || "Failed to load activity";
      }
    },

    conversionPct() {
      return Math.round((this.conversion_rate || 0) * 100);
    },

    sparkSum() {
      return (this.saves_per_day || []).reduce((s, p) => s + (p.count || 0), 0);
    },

    /**
     * Build SVG `points=` string from saves_per_day. Returns "" when empty.
     * Uses a 120x28 viewBox so the polyline scales via preserveAspectRatio.
     */
    sparkline(width = 120, height = 28) {
      const pts = this.saves_per_day || [];
      if (pts.length === 0) return "";
      const max = Math.max(1, ...pts.map(p => p.count || 0));
      const step = pts.length > 1 ? width / (pts.length - 1) : 0;
      return pts.map((p, i) => {
        const x = (i * step).toFixed(1);
        const y = (height - ((p.count || 0) / max) * height).toFixed(1);
        return `${x},${y}`;
      }).join(" ");
    },
  };
}
window.myActivity = myActivity;
