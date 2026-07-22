// Base-template JS bootstrap for every admin page.
//
// Loaded from `templates/base.html` (gated by `@login_required`).
// Reads `window.APP_CONFIG` (inlined server-side from `settings.toml`)
// and primes the in-browser state:
//
//   - On every page load, try to hydrate the in-memory
//     MessageManager from `sessionStorage` via
//     `window._hydrate_from_cache()`. If that returns true, the
//     page renders the cached state on the first frame and no
//     network call happens. The WS connection (started by
//     `app_main.py` and fed by `MessageManager.dispatch`) keeps
//     the cache current from there.
//
//   - On a cache miss (first page load this tab, after logout,
//     or after a `seed()` Refresh that wiped the cache), call
//     `window._seed()`. That fetches `/api/messages` and
//     `/api/config` from Flask, populates the in-memory
//     MessageManager, and writes a fresh cache via the
//     MessageManager's universal `on_change` event.
//
//   - `window.App.registerOnChange(cb)` lets per-page scripts
//     (e.g. /preview, /testing) subscribe to the universal
//     "something changed in MessageManager" event. The
//     `dispatchChange` fan-out is fed by the PyScript-side
//     MessageManager (`app_main.py` wires MqttWsClient →
//     MessageManager.dispatch → on_change → window.App._dispatchChange).
//     One event covers all mutations: WS message envelope, WS
//     config envelope, seed completion, cache hydrate. The
//     page re-renders whatever on its DOM could be affected
//     by any state change.
//
//   - `window.App.getMessages(limit, suppress)` and
//     `window.App.getConfig()` are read APIs that delegate to
//     `window._message_manager` (PyScript-installed).
//
// The `MQTT` status pill (`#mqtt-status`) is updated by
// `app_main.py`'s `_on_status_js` — the JS-side `setStatus` and
// `setPersistenceStatus` helpers are kept here as no-ops for
// backward compat with any code that may have referenced them.
//
// Per-page scripts that need to load on every admin page can
// call `window.App.registerOnChange(...)` to subscribe to
// state changes and `window.App.getMessages(...)` /
// `getConfig()` to read the current buffer.

(function () {
  "use strict";

  function setStatus(state, detail) {
    // Status pill is driven from `app_main.py`'s `_on_status_js`
    // (which calls into the same #mqtt-status element). Kept here
    // as a no-op stub for any code that may still call it.
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
  // event. The PyScript-side `app_main.py._on_change_js` calls
  // `App._dispatchChange` after every MessageManager mutation;
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

  async function waitForAppMain(timeoutMs) {
    // Poll for the in-browser MessageManager proxies. PyScript
    // 2024.9.1 dispatches `py:done` after `<py-script>`'s main
    // module finishes evaluating, but the event-timing is
    // fragile in this version — on a cold load (micropip +
    // tzdata + numpy + Pillow + a top-level `await`), `app.js`
    // was running and giving up well before the proxies were
    // installed, and the `py:done` listener wasn't catching
    // them reliably. Polling the function off `window` is
    // boring and robust: as soon as `app_main.py` finishes
    // line `js.window._hydrate_from_cache = create_proxy(...)`,
    // this resolves. 60s cap bounds a hung PyScript so the
    // page doesn't sit forever on an empty testing table.
    if (typeof timeoutMs !== "number") timeoutMs = 60000;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (
        typeof window._hydrate_from_cache === "function" &&
        typeof window._seed === "function"
      ) {
        return;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  }

  function clearMessageCache() {
    // sessionStorage cache is gone (issue #48, §2.4). Kept as a
    // no-op so legacy callers (`login.html` postMessage) don't
    // throw. Any leftover keys with the `lindsay50:` prefix are
    // wiped defensively — they shouldn't exist, but if an older
    // build wrote them they're not load-bearing any more.
    let wiped = 0;
    try {
      for (let i = sessionStorage.length - 1; i >= 0; i--) {
        const k = sessionStorage.key(i);
        if (k && k.indexOf("lindsay50:") === 0) {
          sessionStorage.removeItem(k);
          wiped += 1;
        }
      }
      if (wiped > 0) {
        console.info("[app] cleared leftover message cache (keys wiped:", wiped, ")");
      }
    } catch (e) {
      console.warn("[app] clearMessageCache failed:", e);
    }
  }

  async function waitForDashboard(timeoutMs) {
    // Poll for the dashboard controller installed by `app_main.py`.
    // The previous version of this shim waited for
    // `_hydrate_from_cache` and `_seed`; under #48 the bootstrap
    // is owned by the per-generation controller, and the only
    // proxy the JS side needs is the controller itself (the seed
    // is awaited internally by `controller.start()`).
    if (typeof timeoutMs !== "number") timeoutMs = 60000;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (typeof window.Dashboard === "object" && window.Dashboard !== null) {
        return;
      }
      await new Promise((r) => setTimeout(r, 50));
    }
  }

  async function init() {
    // Wait for the dashboard controller (installed by `app_main.py`)
    // and kick off the first generation. The controller's
    // `start()` awaits its internal seed + WS connect; we don't
    // need to do anything else here — `app_main.py` wires the
    // MQTT-WS envelope fan-out into the per-generation manager,
    // and the dashboard page's lifecycle controls own subsequent
    // Stop / Restart transitions.
    //
    // On pages WITHOUT the dashboard simulator (Settings, Testing,
    // Messages, archive) `window.Dashboard` is still installed
    // (it's a per-page singleton — `app_main.py` is loaded once
    // per page). The runtime will still spin up; the rAF loop
    // gate (`window.__PREVIEW_TICK_ENABLED__`) is false on those
    // pages because `dashboard_controls.js` is absent.
    await waitForDashboard(60000);
    if (!window.Dashboard || typeof window.Dashboard.start !== "function") {
      console.warn("[app] window.Dashboard not available after 60s; skipping auto-start");
      return;
    }
    try {
      await window.Dashboard.start();
      console.info("[app] dashboard runtime started");
    } catch (e) {
      console.warn("[app] dashboard start failed:", e);
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
    clearMessageCache,
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
