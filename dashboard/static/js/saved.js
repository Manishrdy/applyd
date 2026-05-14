/**
 * /saved page — Alpine component.
 *
 * Same shape as the dashboard but pulls from /api/saved/. Adds inline
 * status changes (queued/applied/skipped/archived) and per-item remove.
 */

const STATUS_TABS = [
  { value: "all",      label: "All" },
  { value: "queued",   label: "Queued" },
  { value: "applied",  label: "Applied" },
  { value: "skipped",  label: "Skipped" },
  { value: "archived", label: "Archived" },
];

const STATUS_CYCLE = ["queued", "applied", "skipped", "archived"];

const STATUS_VARIANT = {
  queued:   "info",
  applied:  "success",
  skipped:  "warning",
  archived: "neutral",
};

function savedPage() {
  return {
    items: [],
    total: 0,
    isLoading: true,
    error: null,
    statusTab: "all",
    STATUS_TABS,

    async init() {
      const p = new URLSearchParams(window.location.search);
      if (p.has("status") && STATUS_TABS.find(t => t.value === p.get("status"))) {
        this.statusTab = p.get("status");
      }
      await this.fetchSaved();
    },

    async fetchSaved() {
      this.isLoading = true;
      this.error = null;
      try {
        const url = this.statusTab === "all"
          ? "/api/saved/?limit=500"
          : `/api/saved/?status=${this.statusTab}&limit=500`;
        const r = await fetch(url);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const data = await r.json();
        this.items = data.saved;
        this.total = data.total;
      } catch (e) {
        this.error = e.message || "Failed to load saved jobs";
      } finally {
        this.isLoading = false;
      }
    },

    setTab(value) {
      if (value === this.statusTab) return;
      this.statusTab = value;
      const url = value === "all" ? "/saved" : `/saved?status=${value}`;
      window.history.replaceState({}, "", url);
      this.fetchSaved();
    },

    async cycleStatus(item) {
      const i = STATUS_CYCLE.indexOf(item.status);
      const next = STATUS_CYCLE[(i + 1) % STATUS_CYCLE.length];
      const prev = item.status;
      item.status = next;
      try {
        const r = await fetch(`/api/saved/${item.id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ status: next }),
        });
        if (!r.ok) throw new Error();
        // If a status filter is active and we just moved out of it, drop the row.
        if (this.statusTab !== "all" && next !== this.statusTab) {
          this.items = this.items.filter(x => x.id !== item.id);
          this.total = Math.max(0, this.total - 1);
        }
      } catch {
        item.status = prev;
      }
    },

    async remove(item) {
      const idx = this.items.findIndex(x => x.id === item.id);
      if (idx < 0) return;
      const removed = this.items.splice(idx, 1)[0];
      this.total = Math.max(0, this.total - 1);
      try {
        const r = await fetch(`/api/saved/${item.id}`, { method: "DELETE" });
        if (!r.ok) throw new Error();
        window.dispatchEvent(new CustomEvent("applyd:saved-changed", {
          detail: { delta: -1 },
        }));
      } catch {
        // restore on failure
        this.items.splice(idx, 0, removed);
        this.total += 1;
      }
    },

    async saveNotes(item, value) {
      const trimmed = (value || "").trim();
      if (trimmed === (item.notes || "").trim()) return;
      const prev = item.notes;
      item.notes = trimmed || null;
      try {
        const r = await fetch(`/api/saved/${item.id}`, {
          method: "PATCH",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ notes: trimmed }),
        });
        if (!r.ok) throw new Error();
      } catch {
        item.notes = prev;
      }
    },

    statusVariant(status) {
      return STATUS_VARIANT[status] || "neutral";
    },

    formatSalary(item) {
      const min = item.salary_min_usd_annual;
      const max = item.salary_max_usd_annual;
      if (min == null && max == null) return item.salary_summary || "—";
      const fmt = v => `$${Math.round(v / 1000)}k`;
      if (min && max && min !== max) return `${fmt(min)}–${fmt(max)}`;
      return fmt(max || min);
    },

    formatTimeAgo(iso) {
      // Server returns SQLite-format "YYYY-MM-DD HH:MM:SS" without a TZ marker
      // (UTC). Normalize to T-separated + Z so JS doesn't parse as local time.
      if (!iso) return "—";
      let s = String(iso).replace(" ", "T");
      if (!/[Zz]|[+-]\d{2}:?\d{2}$/.test(s)) s += "Z";
      const ts = new Date(s);
      if (isNaN(ts.getTime())) return "—";
      const secs = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 1000));
      if (secs < 60)    return `${secs}s ago`;
      if (secs < 3600)  return `${Math.floor(secs / 60)}m ago`;
      if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
      const days = Math.floor(secs / 86400);
      if (days < 30)    return `${days}d ago`;
      return `${Math.floor(days / 30)}mo ago`;
    },
  };
}

window.savedPage = savedPage;
