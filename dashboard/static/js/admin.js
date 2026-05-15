/* Shared helpers for admin Alpine components.
 *
 * Provides:
 *   window.adminApi.get(path)
 *   window.adminApi.post(path, formData | object)   -- merges CSRF token
 *   window.adminApi.csrfToken()
 *   window.adminToast(message, variant)
 *
 * Keep this small. Per-page state lives inline in each template's x-data
 * block, mirroring how dashboard.js / settings.js are structured.
 */
(function () {
  "use strict";

  function readCookie(name) {
    var cookies = document.cookie.split(";");
    for (var i = 0; i < cookies.length; i++) {
      var c = cookies[i].trim();
      if (c.indexOf(name + "=") === 0) return decodeURIComponent(c.slice(name.length + 1));
    }
    return "";
  }

  function csrfToken() {
    return readCookie("applyd_csrf");
  }

  async function get(path) {
    var res = await fetch(path, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  }

  async function post(path, body) {
    var form = new FormData();
    if (body instanceof FormData) {
      body.forEach(function (v, k) {
        form.append(k, v);
      });
    } else if (body && typeof body === "object") {
      Object.keys(body).forEach(function (k) {
        if (body[k] === undefined || body[k] === null) return;
        form.append(k, String(body[k]));
      });
    }
    if (!form.has("csrf_token")) form.append("csrf_token", csrfToken());

    var res = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      body: form,
    });
    var payload = null;
    try { payload = await res.json(); } catch (_) { /* ignore */ }
    if (!res.ok) {
      var err = new Error((payload && payload.detail) || ("HTTP " + res.status));
      err.status = res.status;
      err.payload = payload;
      throw err;
    }
    return payload;
  }

  function toast(message, variant) {
    var root = document.getElementById("admin-toast-root");
    if (!root) {
      root = document.createElement("div");
      root.id = "admin-toast-root";
      root.className = "fixed bottom-6 right-6 z-50 space-y-2";
      document.body.appendChild(root);
    }
    var el = document.createElement("div");
    el.className =
      "toast text-sm px-3 py-2 rounded-base border shadow-sm " +
      (variant === "error"
        ? "border-danger/40 bg-danger/10 text-danger"
        : variant === "success"
        ? "border-success/40 bg-success/10 text-success"
        : "border-border-base bg-surface text-text");
    el.textContent = message;
    root.appendChild(el);
    setTimeout(function () { el.remove(); }, 4500);
  }

  window.adminApi = { get: get, post: post, csrfToken: csrfToken };
  window.adminToast = toast;
})();
