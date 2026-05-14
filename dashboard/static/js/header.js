/**
 * Header-resident Alpine factories — loaded on every page.
 *
 *   savedCounter()  → small badge next to the bookmark icon
 *   commandPalette() → cmd-K modal: jump-to nav, theme toggle, jump-to-search
 */

/* ============================================================
 * savedCounter — fetches /api/saved/ once on init.
 * Listens for "applyd:saved-changed" on window so the dashboard
 * can update the count without a refetch.
 * ============================================================ */
function savedCounter() {
  return {
    count: 0,

    init() {
      this.refresh();
      window.addEventListener("applyd:saved-changed", (e) => {
        if (typeof e.detail?.delta === "number") {
          this.count = Math.max(0, this.count + e.detail.delta);
        } else {
          this.refresh();
        }
      });
    },

    async refresh() {
      try {
        const r = await fetch("/api/saved/?limit=1");
        if (!r.ok) return;
        const data = await r.json();
        this.count = data.total || 0;
      } catch {}
    },
  };
}

/* ============================================================
 * commandPalette — cmd-K (or ctrl-K) opens a modal.
 *   - Top input filters items by fuzzy substring match
 *   - Up/Down arrows navigate, Enter activates, Esc closes
 *   - Items: navigation, theme toggle, "search for X"
 * ============================================================ */
function commandPalette() {
  const NAV_ITEMS = [
    { id: "nav-home",     label: "Jobs",            hint: "/",          group: "Navigate", action: () => location.assign("/") },
    { id: "nav-saved",    label: "Saved jobs",      hint: "/saved",     group: "Navigate", action: () => location.assign("/saved") },
    { id: "nav-stats",    label: "Stats",           hint: "/stats",     group: "Navigate", action: () => location.assign("/stats") },
    { id: "nav-settings", label: "Settings",        hint: "/settings",  group: "Navigate", action: () => location.assign("/settings") },
    { id: "nav-scrape",   label: "Scrape (manual)", hint: "/scrape",    group: "Navigate", action: () => location.assign("/scrape") },
    { id: "nav-style",    label: "Styleguide",      hint: "/styleguide",group: "Navigate", action: () => location.assign("/styleguide") },
    { id: "nav-docs",     label: "API docs",        hint: "/docs",      group: "Navigate", action: () => location.assign("/docs") },
    { id: "act-theme",    label: "Toggle light / dark theme", hint: "⌘+K then T", group: "Actions",
      action: () => window.applydTheme.toggle() },
  ];

  return {
    open: false,
    query: "",
    cursor: 0,

    init() {
      window.addEventListener("keydown", (e) => {
        const isMod = e.metaKey || e.ctrlKey;
        if (isMod && e.key.toLowerCase() === "k") {
          e.preventDefault();
          this.show();
        } else if (this.open && e.key === "Escape") {
          this.close();
        }
      });
    },

    show() {
      this.open = true;
      this.query = "";
      this.cursor = 0;
      this.$nextTick(() => this.$refs.input?.focus());
    },

    close() { this.open = false; },

    items() {
      const q = this.query.trim().toLowerCase();
      const base = NAV_ITEMS.slice();
      if (q) {
        // Always include a "Search jobs for: <q>" action when typing
        base.unshift({
          id: "search-jobs",
          label: `Search jobs for "${this.query}"`,
          hint: "Enter",
          group: "Actions",
          action: () => location.assign("/?q=" + encodeURIComponent(this.query)),
        });
      }
      if (!q) return base;
      return base.filter(it =>
        it.label.toLowerCase().includes(q) || (it.hint || "").toLowerCase().includes(q)
      );
    },

    onKey(e) {
      const items = this.items();
      if (e.key === "ArrowDown") {
        e.preventDefault();
        this.cursor = Math.min(this.cursor + 1, items.length - 1);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        this.cursor = Math.max(this.cursor - 1, 0);
      } else if (e.key === "Enter") {
        e.preventDefault();
        const item = items[this.cursor];
        if (item) {
          this.close();
          item.action();
        }
      }
    },

    activate(idx) {
      const item = this.items()[idx];
      if (item) {
        this.close();
        item.action();
      }
    },
  };
}

window.savedCounter = savedCounter;
window.commandPalette = commandPalette;
