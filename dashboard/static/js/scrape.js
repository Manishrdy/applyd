function scrapePage() {
  return {
    catalog: null,
    catalogError: null,

    selected: new Set(),
    atsFilter: '',
    maxCompanies: null,
    incrementalEnabled: false,
    incrementalDays: 7,

    presets: [],
    activePresetId: 0,
    presetName: '',

    coverage: {},
    coverageDetailAts: null,
    coverageRows: [],

    activeRunId: null,
    activeRun: null,
    perAts: {},
    es: null,

    runs: [],
    runsError: null,

    startError: null,
    cancelError: null,

    startedAt: null,
    elapsedMs: 0,
    _tickerHandle: null,

    async init() {
      await Promise.all([this.fetchCatalog(), this.fetchRuns(), this.fetchPresets()]);
      await this.fetchCoverage();
      const live = this.runs.find(r => r.status === 'running' || r.status === 'queued');
      if (live) {
        this.activeRunId = live.id;
        await this.refreshActive();
        this.connectSSE(live.id);
      }
    },

    async fetchCatalog() {
      try {
        const r = await fetch('/api/scrape/ats');
        if (!r.ok) throw new Error(`http ${r.status}`);
        this.catalog = await r.json();
        const recommended = this.catalog.recommended || [];
        const cap = this.catalog.max_ats_per_run ?? 5;
        const allowed = new Set(this.catalog.allowed || []);
        for (const a of recommended) {
          if (this.selected.size >= cap) break;
          if (allowed.has(a)) this.selected.add(a);
        }
        if (this.maxCompanies === null) this.maxCompanies = this.catalog.default_max_companies ?? null;
        this.incrementalDays = this.catalog.default_incremental_days ?? 7;
      } catch (e) {
        this.catalogError = String(e);
      }
    },

    async fetchPresets() {
      try {
        const r = await fetch('/api/scrape/presets');
        if (!r.ok) throw new Error(`http ${r.status}`);
        const json = await r.json();
        this.presets = json.presets || [];
      } catch (_) { this.presets = []; }
    },

    async savePreset() {
      const payload = {
        name: this.presetName.trim(),
        ats_requested: this.selectedList(),
        max_companies_per_ats: this.maxCompanies || null,
        incremental_enabled: this.incrementalEnabled,
        incremental_days: this.incrementalEnabled ? this.incrementalDays : null,
        notes: null,
        is_default: false,
      };
      if (!payload.name) return;
      const r = await fetch('/api/scrape/presets', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
      });
      if (!r.ok) return;
      const { preset } = await r.json();
      this.activePresetId = preset.id;
      this.presetName = preset.name;
      await this.fetchPresets();
    },

    async deletePreset(id) {
      if (!id) return;
      await fetch(`/api/scrape/presets/${id}`, { method: 'DELETE' });
      if (this.activePresetId === id) this.activePresetId = 0;
      await this.fetchPresets();
    },

    loadPreset(id) {
      const p = this.presets.find(x => x.id === id);
      if (!p) return;
      this.selected = new Set(p.ats_requested || []);
      this.maxCompanies = p.max_companies_per_ats || null;
      this.incrementalEnabled = !!p.incremental_enabled;
      this.incrementalDays = p.incremental_days || (this.catalog?.default_incremental_days ?? 7);
      this.presetName = p.name || '';
      this.fetchCoverage();
    },

    canSavePreset() {
      return this.presetName.trim().length > 0 && this.selected.size > 0;
    },

    async fetchCoverage() {
      const ats = this.selectedList();
      const q = ats.map(a => `ats=${encodeURIComponent(a)}`).join('&');
      try {
        const r = await fetch(`/api/scrape/coverage${q ? `?${q}` : ''}`);
        if (!r.ok) throw new Error();
        const json = await r.json();
        this.coverage = json.coverage || {};
      } catch (_) {
        this.coverage = {};
      }
    },

    async openCoverageDetail(ats) {
      this.coverageDetailAts = ats;
      try {
        const r = await fetch(`/api/scrape/coverage/${encodeURIComponent(ats)}?limit=400`);
        if (!r.ok) throw new Error();
        const json = await r.json();
        this.coverageRows = json.rows || [];
      } catch (_) {
        this.coverageRows = [];
      }
    },

    bucketPct(ats, key) {
      const c = this.coverage?.[ats] || {};
      const total = (c.never || 0) + (c['0_1d'] || 0) + (c['2_7d'] || 0) + (c['8_30d'] || 0) + (c.gt_30d || 0);
      if (!total) return 0;
      return Math.round(((c[key] || 0) / total) * 100);
    },

    async fetchRuns() {
      try {
        const r = await fetch('/api/scrape/runs?limit=50');
        if (!r.ok) throw new Error(`http ${r.status}`);
        const json = await r.json();
        this.runs = json.runs || [];
        this.runsError = null;
      } catch (e) {
        this.runsError = String(e);
      }
    },

    isAllowed(ats) { return (this.catalog?.allowed || []).includes(ats); },
    maxAts() { return this.catalog?.max_ats_per_run ?? 5; },
    atLimit() { return this.selected.size >= this.maxAts(); },
    canSelectMore() { return this.selected.size < this.maxAts(); },

    toggleAts(ats) {
      if (!this.isAllowed(ats)) return;
      if (this.activeRunId !== null) return;
      if (this.selected.has(ats)) { this.selected.delete(ats); this.fetchCoverage(); return; }
      if (this.atLimit()) return;
      this.selected.add(ats);
      this.fetchCoverage();
    },

    selectedList() { return [...this.selected].sort(); },

    visibleAts() {
      const all = this.catalog?.available || [];
      const q = (this.atsFilter || '').trim().toLowerCase();
      if (!q) return all;
      return all.filter(a => a.toLowerCase().includes(q));
    },

    selectVisible() {
      if (this.activeRunId !== null) return;
      const list = this.visibleAts();
      for (const a of list) {
        if (this.atLimit()) break;
        if (!this.isAllowed(a)) continue;
        this.selected.add(a);
      }
      this.fetchCoverage();
    },

    clearSelected() {
      if (this.activeRunId !== null) return;
      this.selected.clear();
      this.fetchCoverage();
    },

    tileTitle(ats) {
      if (!this.isAllowed(ats)) return 'Not in allow-list (settings.local_scraper_allowed_ats)';
      if (this.atLimit() && !this.selected.has(ats)) return `Max ${this.maxAts()} ATS per run`;
      if (this.activeRunId !== null) return 'A run is in progress';
      const cov = this.coverage?.[ats];
      if (!cov) return ats;
      const total = this.coverageTotal(ats);
      return `${ats} · ${total} companies · ${cov.never || 0} never scraped`;
    },

    /** Returns a Tailwind class qualifier for the health dot on an ATS tile.
     * Based on the "never scraped" share of companies. */
    coverageHealth(ats) {
      const cov = this.coverage?.[ats];
      if (!cov) return '';
      const total = this.coverageTotal(ats);
      if (!total) return '';
      const never = cov.never || 0;
      const stale = cov.gt_30d || 0;
      const ratio = (never + stale) / total;
      if (ratio >= 0.6) return 'tile-dot-stale';
      if (ratio >= 0.25) return 'tile-dot-warn';
      return 'tile-dot-ok';
    },

    coverageTotal(ats) {
      const c = this.coverage?.[ats] || {};
      return (c.never || 0) + (c['0_1d'] || 0) + (c['2_7d'] || 0) + (c['8_30d'] || 0) + (c.gt_30d || 0);
    },

    coverageTooltip(ats) {
      const c = this.coverage?.[ats] || {};
      return `never ${c.never||0} · 0-1d ${c['0_1d']||0} · 2-7d ${c['2_7d']||0} · 8-30d ${c['8_30d']||0} · >30d ${c.gt_30d||0}`;
    },

    /** Live-readable label combining selected ATS count + max-companies cap. */
    estimateCompaniesLabel() {
      if (!this.selected.size) return '—';
      if (this.maxCompanies && this.maxCompanies > 0) {
        const cap = this.selected.size * Number(this.maxCompanies);
        return `≤ ${cap.toLocaleString()}`;
      }
      // Sum total companies across selected ATS from coverage, when known.
      let total = 0;
      let known = 0;
      for (const a of this.selected) {
        const t = this.coverageTotal(a);
        if (t > 0) { total += t; known++; }
      }
      if (known === this.selected.size && total > 0) {
        return `≈ ${total.toLocaleString()}`;
      }
      return 'all';
    },

    /** Aggregate counters across the live perAts map. */
    liveTotals() {
      let total = 0, completed = 0, failed = 0, scraped = 0,
          written = 0, inserted = 0, updated = 0;
      for (const a of this.selected) {
        const p = this.perAts[a] || {};
        total      += p.companies_total || 0;
        completed  += (p.companies_succeeded || 0) + (p.companies_failed || 0);
        failed     += p.companies_failed || 0;
        scraped    += p.rows_scraped || 0;
        written    += p.rows_written || 0;
        inserted   += p.rows_inserted || 0;
        updated    += p.rows_updated || 0;
      }
      return { total, completed, failed, scraped, written, inserted, updated };
    },

    /** Short summary of the most recent finished run, for the header pill. */
    lastRunSummary() {
      const last = (this.runs || []).find(r => r.finished_at);
      if (!last) return '';
      const when = (() => {
        try { return new Date(last.finished_at).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }); }
        catch { return last.finished_at; }
      })();
      return `${when} · ${last.status}`;
    },

    canStart() {
      return this.selected.size > 0 && this.selected.size <= this.maxAts() && this.activeRunId === null && !this.catalogError;
    },

    async start() {
      if (!this.canStart()) return;
      this.startError = null;
      try {
        const r = await fetch('/api/scrape/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            ats: this.selectedList(),
            max_companies_per_ats: this.maxCompanies || null,
            triggered_by: 'manual_ui',
            incremental_enabled: this.incrementalEnabled,
            incremental_days: this.incrementalEnabled ? this.incrementalDays : null,
            preset_id: this.activePresetId || null,
          }),
        });
        if (!r.ok) {
          const body = await r.json().catch(() => ({detail: 'unknown error'}));
          throw new Error(`${r.status}: ${body.detail || 'unknown error'}`);
        }
        const { run_id } = await r.json();
        this.activeRunId = run_id;
        await this.refreshActive();
        this.connectSSE(run_id);
        await this.fetchRuns();
      } catch (e) {
        this.startError = String(e).replace(/^Error:\s*/, '');
      }
    },

    async cancel() {
      if (!this.activeRunId) return;
      this.cancelError = null;
      try {
        const r = await fetch(`/api/scrape/runs/${this.activeRunId}/cancel`, {method: 'POST'});
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw new Error(body.detail || `http ${r.status}`);
        }
      } catch (e) {
        this.cancelError = String(e).replace(/^Error:\s*/, '');
      }
    },

    async refreshActive() {
      if (!this.activeRunId) return;
      try {
        const r = await fetch(`/api/scrape/runs/${this.activeRunId}`);
        if (!r.ok) throw new Error(`http ${r.status}`);
        this.activeRun = await r.json();
        for (const ats of this.activeRun.per_ats) this.perAts[ats.ats] = ats;
        if (!this.startedAt && this.activeRun.started_at) {
          this.startedAt = new Date(this.activeRun.started_at).getTime();
          this._startTicker();
        }
      } catch (_) {}
    },

    connectSSE(runId) {
      if (this.es) { try { this.es.close(); } catch (_) {} }
      const es = new EventSource(`/api/scrape/runs/${runId}/stream`);
      this.es = es;
      es.onmessage = async (e) => {
        let evt;
        try { evt = JSON.parse(e.data); } catch { return; }
        const k = evt.event;
        if (k === 'run_running' || k === 'run_already_finished') {
          await this.refreshActive();
        } else if (k === 'ats_running' || k === 'ats_progress' || k === 'ats_progress_snapshot') {
          const cur = this.perAts[evt.ats] || {ats: evt.ats, status: 'running'};
          this.perAts[evt.ats] = {
            ...cur,
            status: k === 'ats_running' ? 'running' : (cur.status || 'running'),
            companies_total: evt.companies_total ?? cur.companies_total ?? 0,
            companies_succeeded: evt.companies_succeeded ?? cur.companies_succeeded ?? 0,
            companies_failed: evt.companies_failed ?? cur.companies_failed ?? 0,
            rows_scraped: evt.rows_scraped ?? cur.rows_scraped ?? 0,
            rows_written: evt.rows_written ?? cur.rows_written ?? 0,
            rows_inserted: evt.rows_inserted ?? cur.rows_inserted ?? 0,
            rows_updated: evt.rows_updated ?? cur.rows_updated ?? 0,
            phase: evt.phase ?? cur.phase,
            phase_started_at: evt.phase_started_at ?? cur.phase_started_at,
            eta_seconds: evt.eta_seconds ?? cur.eta_seconds,
            throughput_cpm: evt.throughput_cpm ?? cur.throughput_cpm,
            last_event: evt.last_event ?? cur.last_event,
          };
        } else if (k === 'ats_finished') {
          await this.refreshActive();
        } else if (k === 'run_finished') {
          await this.refreshActive();
          this._stopTicker();
          try { es.close(); } catch (_) {}
          this.es = null;
          setTimeout(async () => {
            this.activeRunId = null;
            this.activeRun = null;
            this.perAts = {};
            this.startedAt = null;
            this.elapsedMs = 0;
            await this.fetchRuns();
            await this.fetchCoverage();
          }, 1500);
        }
      };
    },

    _startTicker() {
      if (this._tickerHandle) return;
      this._tickerHandle = setInterval(() => {
        if (this.startedAt) this.elapsedMs = Date.now() - this.startedAt;
      }, 1000);
    },
    _stopTicker() {
      if (this._tickerHandle) { clearInterval(this._tickerHandle); this._tickerHandle = null; }
    },

    fmtElapsed(ms) {
      if (!ms || ms < 0) return '0s';
      const s = Math.floor(ms / 1000);
      if (s < 60) return `${s}s`;
      const m = Math.floor(s / 60), rs = s % 60;
      if (m < 60) return `${m}m ${rs}s`;
      const h = Math.floor(m / 60), rm = m % 60;
      return `${h}h ${rm}m`;
    },
    fmtDuration(startedIso, finishedIso) {
      if (!startedIso) return '—';
      const s = new Date(startedIso).getTime();
      const f = finishedIso ? new Date(finishedIso).getTime() : Date.now();
      return this.fmtElapsed(f - s);
    },
    fmtTime(iso) { if (!iso) return '—'; try { return new Date(iso).toLocaleString(); } catch { return iso; } },
    fmtNum(n) { if (n == null) return '—'; return Number(n).toLocaleString(); },
    fmtEta(sec) {
      if (sec == null) return '—';
      if (sec <= 0) return '0s';
      return this.fmtElapsed(sec * 1000);
    },
    pct(num, denom) { if (!denom) return 0; return Math.min(100, Math.round((num / denom) * 100)); },
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
  };
}
