// /scrape/runs/{id} — per-run detail. Mostly read-only; expandable logs.

function scrapeDetailPage(runId) {
  return {
    runId,
    run: null,
    loadError: null,
    logs: {},        // { ats: { text, loading, error } }

    async init() {
      await this.fetch();
      // If still running, poll every 2s for a smooth-ish refresh.
      if (this.run && (this.run.status === 'running' || this.run.status === 'queued')) {
        this._pollHandle = setInterval(() => this.fetch(), 2000);
      }
    },

    async fetch() {
      try {
        const r = await fetch(`/api/scrape/runs/${this.runId}`);
        if (!r.ok) throw new Error(`http ${r.status}`);
        this.run = await r.json();
        this.loadError = null;
        if (this.run.status !== 'running' && this.run.status !== 'queued' && this._pollHandle) {
          clearInterval(this._pollHandle); this._pollHandle = null;
        }
      } catch (e) { this.loadError = String(e); }
    },

    async toggleLog(ats) {
      if (this.logs[ats] && this.logs[ats].open) {
        this.logs[ats].open = false;
        return;
      }
      this.logs[ats] = { open: true, loading: true, text: '', error: null, copied: false };
      try {
        const r = await fetch(`/api/scrape/runs/${this.runId}/logs/${ats}?tail_lines=500`);
        if (!r.ok) throw new Error(`http ${r.status}`);
        const text = await r.text();
        this.logs[ats] = { open: true, loading: false, text, error: null, copied: false };
      } catch (e) {
        this.logs[ats] = { open: true, loading: false, text: '', error: String(e), copied: false };
      }
    },

    async copyLog(ats) {
      const entry = this.logs[ats];
      if (!entry || !entry.text) return;
      try {
        await navigator.clipboard.writeText(entry.text);
        this.logs[ats] = { ...entry, copied: true };
        setTimeout(() => {
          const cur = this.logs[ats];
          if (cur) this.logs[ats] = { ...cur, copied: false };
        }, 1500);
      } catch (_) {}
    },

    fmtNum(n) { if (n == null) return '—'; return Number(n).toLocaleString(); },
    fmtTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString(); } catch { return iso; } },
    fmtDuration(s, f) {
      if (!s) return '—';
      const ms = (f ? new Date(f).getTime() : Date.now()) - new Date(s).getTime();
      const sec = Math.floor(ms / 1000);
      if (sec < 60) return `${sec}s`;
      const m = Math.floor(sec / 60), rs = sec % 60;
      if (m < 60) return `${m}m ${rs}s`;
      const h = Math.floor(m / 60), rm = m % 60;
      return `${h}h ${rm}m`;
    },
    statusClass(status) {
      switch (status) {
        case 'succeeded': return 'badge badge-success';
        case 'running':   return 'badge badge-info';
        case 'queued':    return 'badge';
        case 'pending':   return 'badge';
        case 'partial':   return 'badge badge-warning';
        case 'failed':    return 'badge badge-danger';
        case 'cancelled': return 'badge badge-warning';
        case 'skipped':   return 'badge badge-warning';
        default:          return 'badge';
      }
    },
    pct(num, denom) {
      if (!denom) return 0;
      return Math.min(100, Math.round((num / denom) * 100));
    },
    viewJobsUrl(ats) {
      // Drill into the dashboard scoped to the URL set this run captured for
      // this ATS. Backed by scrape_run_url — immune to later writes (cron,
      // other runs) bumping jobs.updated_at on the same URLs. Earlier
      // versions used updated_at windows which silently went stale the next
      // time the daily manifest cron fired.
      if (!this.run) return '#';
      const params = new URLSearchParams({
        ats: ats.ats,
        scrape_run_id: this.run.id,
        posted_hours: '0',   // jobs from this run regardless of post age
      });
      return `/?${params.toString()}`;
    },
  };
}
