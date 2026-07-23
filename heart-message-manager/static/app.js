// Base-template JS bootstrap for every admin page.
//
// Loaded from `templates/base.html` (gated by `@login_required`).
// Reads `window.APP_CONFIG` (inlined server-side from `settings.toml`)
// and primes the in-browser state:
//
//   - The in-memory MessageManager is per-tab (issue #48, simplified
//     2026-07-23). Each fresh page load starts a fresh
//     `install_runtime()` that builds its own MessageManager and
//     exposes it as `window._message_manager`. No sessionStorage /
//     IndexedDB hydration is performed here — every page load is a
//     fresh start, the same way restarting the Pi resets its
//     display state.
//
//   - `window.App.registerOnChange(cb)` lets per-page scripts
//     (e.g. /preview, /testing) subscribe to the universal
//     "something changed in MessageManager" event. The
//     `dispatchChange` fan-out is fed by the PyScript-side
//     MessageManager (`dashboard_runtime.py` wires MqttWsClient →
//     MessageManager.dispatch → on_change → window.App._dispatchChange).
//     One event covers all mutations: WS message envelope, WS
//     config envelope, seed completion. The page re-renders
//     whatever on its DOM could be affected by any state change.
//
//   - `window.App.getMessages(limit, suppress)` and
//     `window.App.getConfig()` are read APIs that delegate to
//     `window._message_manager` (PyScript-installed).
//
// The MQTT status pill is driven from `dashboard_runtime.py`'s
// `_on_status_py` — the JS-side `setStatus` and `setPersistenceStatus`
// helpers are kept here as no-ops for backward compat with any code
// that may have referenced them.
//
// Per-page scripts that need to load on every admin page can
// call `window.App.registerOnChange(...)` to subscribe to
// state changes and `window.App.getMessages(...)` / `getConfig()`
// to read the current buffer.

(function () {
  "use strict";

  function setStatus(state, detail) {
    // Status pill is driven from `dashboard_runtime.py`'s
    // `_on_status_py` (which calls into the same pill element).
    // Kept here as a no-op stub for any code that may still call it.
    void state;
    void detail;
  }

  function setPersistenceStatus(ok, error) {
    // IndexedDB is gone. The "Persistence unavailable" pill is no
    // longer relevant — we always have the in-memory MessageManager
    // and the server-side (S3 + SQLite) source of truth.
    void ok;
    void error;
  }

  // Per-page callback registration for the universal change
  // event. The PyScript-side `dashboard_runtime._on_change_js`
  // calls `App._dispatchChange` after every MessageManager mutation;
  // the registered callbacks here fan that out to per-page
  // listeners (e.g. /testing's `reRender`).
  const onChangeCallbacks = [];

  function registerOnChange(cb) {
    if (typeof cb === "function") {
      onChangeCallbacks.push(cb);
    }
  }

  function dispatchChange() {
    for (const cb of onChangeCallbacks) {
      try {
        cb();
      } catch (e) {
        console.error("onChange callback error:", e);
      }
    }
  }

  async function getMessages(limit, suppress) {
    if (suppress === undefined) suppress = true;
    if (!window._message_manager) return [];
    try {
      return await window._message_manager.get_messages(limit, suppress);
    } catch (e) {
      console.warn("getMessages failed:", e);
      return [];
    }
  }

  async function getConfigNow() {
    if (!window._message_manager) return null;
    try {
      return await window._message_manager.get_config();
    } catch (e) {
      console.warn("getConfig failed:", e);
      return null;
    }
  }

  async function waitForMessageManager(timeoutMs) {
    // Poll for the in-browser MessageManager installed by
    // `dashboard_runtime.install_runtime()`. PyScript 2024.9.1
    // dispatches `py:done` after `<py-script>`'s main module
    // finishes evaluating, but the event-timing is fragile in
    // this version — on a cold load (micropip + tzdata + numpy +
    // Pillow + a top-level `await`), `app.js` was running and
    // giving up well before the proxies were installed, and the
    // `py:done` listener wasn't catching them reliably. Polling
    // the function off `window` is boring and robust: as soon
    // as `dashboard_runtime.py` finishes
    // `js.window._message_manager = mm`, this resolves. 60s cap
    // bounds a hung PyScript so the page doesn't sit forever on
    // an empty testing table.
    if (typeof timeoutMs !== "number") timeoutMs = 60000;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (window._message_manager && typeof window._seed === "function") {
        return;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  }

  async function init() {
    // Wait for the page-load runtime (installed by
    // `dashboard_runtime.install_runtime()` from `app_main.py`)
    // and seed the in-memory buffer. There is no Start/Stop
    // toggle — the runtime is built ONCE per page load; refresh
    // restarts it. The MQTT-WS client started inside
    // `install_runtime()` is already pumping envelopes; this
    // wait just confirms the MessageManager is wired up before
    // we hit `/api/messages` for the initial hydrate.
    //
    // On pages WITHOUT the dashboard simulator (Settings, Testing,
    // Messages, archive) the runtime is still installed
    // (`app_main.py` is loaded once per page). The seed still
    // runs — it's the per-page JSON source for those views.
    await waitForMessageManager(60000);
    if (!window._message_manager) {
      console.warn("[app] window._message_manager not available after 60s; skipping seed");
      return;
    }
    // Auto-seed the in-memory buffer. `seed()` is idempotent —
    // it clears the buffer first, so calling it on every page
    // load is safe. The resulting `on_change` fans out to
    // `App._dispatchChange()` so any registered listener
    // (dashboard_recent's table re-render, testing's reRender,
    // etc.) updates without polling.
    if (typeof window._seed === "function") {
      try {
        await window._seed();
        console.info("[app] in-memory buffer seeded");
      } catch (e) {
        console.warn("[app] seed failed:", e);
      }
    }
  }

  // expose the registration surface for per-page scripts.
  // `getMessages` / `getConfig` are JS-side wrappers that delegate
  // to the PyScript-side MessageManager; `registerOnChange` and
  // `_dispatchChange` form the universal state-change event
  // surface (see top-of-file comment).
  window.App = {
    registerOnChange,
    getMessages,
    getConfig: getConfigNow,
    // PyScript-side fan-out entry point. The MessageManager's
    // on_change callback calls this; per-page listeners
    // (e.g. testing.html's `reRender`) are reached via
    // `dispatchChange`.
    _dispatchChange: dispatchChange,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();