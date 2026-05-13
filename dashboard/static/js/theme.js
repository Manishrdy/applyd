/**
 * Theme toggle — Alpine-free bootstrap.
 *
 * The inline script in base.html applies the theme BEFORE first paint
 * (FOUC prevention). This file exposes the runtime API for the toggle button.
 *
 * Source-of-truth precedence:
 *   1. localStorage["theme"]   — explicit user choice, persists
 *   2. prefers-color-scheme    — system default, used on first visit
 *
 * Adding/removing the `dark` class on <html> drives all token-aware styles.
 */

(function () {
  const STORAGE_KEY = "applyd:theme";

  function readSystemPref() {
    return window.matchMedia("(prefers-color-scheme: dark)").matches
      ? "dark"
      : "light";
  }

  function readStored() {
    try {
      const v = localStorage.getItem(STORAGE_KEY);
      return v === "dark" || v === "light" ? v : null;
    } catch {
      return null;
    }
  }

  function writeStored(mode) {
    try {
      localStorage.setItem(STORAGE_KEY, mode);
    } catch {}
  }

  function apply(mode) {
    const root = document.documentElement;
    if (mode === "dark") {
      root.classList.add("dark");
    } else {
      root.classList.remove("dark");
    }
    root.dataset.theme = mode;
  }

  function current() {
    return document.documentElement.classList.contains("dark") ? "dark" : "light";
  }

  function set(mode) {
    apply(mode);
    writeStored(mode);
  }

  function toggle() {
    set(current() === "dark" ? "light" : "dark");
  }

  // Track system preference changes when the user has NOT made an explicit choice.
  const systemQuery = window.matchMedia("(prefers-color-scheme: dark)");
  systemQuery.addEventListener("change", (e) => {
    if (readStored() === null) {
      apply(e.matches ? "dark" : "light");
    }
  });

  window.applydTheme = { current, set, toggle, readSystemPref, readStored };
})();
