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

  async function waitForPyDone(timeoutMs) {
    // PyScript 2024.9.x fires `py:done` on each <py-script>
    // element after its main module finishes evaluating —
    // i.e. after all top-level statements in `app_main.py`
    // have run and the proxies (window._seed,
    // window._hydrate_from_cache, etc.) are installed.
    // Polling for a function on `window` works in the warm
    // case, but on a cold load `app.js` runs well before
    // `app_main.py` finishes evaluating (PyScript is still
    // in the "Loading micropip, packaging, tzdata" phase).
    // A 5s polling window wasn't long enough on cold loads
    // — `app.js` would give up, log "neither ... appeared",
    // and the page would silently render with no buffer
    // state. `py:done` is the canonical signal that the
    // main module is ready.
    if (typeof timeoutMs !== "number") timeoutMs = 30000;
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        document.removeEventListener("py:done", finish);
        resolve();
      };
      document.addEventListener("py:done", finish, { once: true });
      setTimeout(finish, timeoutMs);
    });
  }

  async function init() {
    // Two-step boot. PyScript loads asynchronously, so we
    // wait for `py:done` (the canonical "main module
    // finished evaluating" signal) before reading any
    // proxies off `window`. Without this, on a cold load
    // `app.js` races PyScript's bootstrap and gives up
    // before the proxies are installed. The 30s cap is
    // generous — a normal cold load is a few seconds — but
    // bounds a hung PyScript so the page doesn't sit
    // forever on an empty testing/preview table.
    //
    // 1. Try the sessionStorage cache. If the previous page
    //    load (within this tab) wrote a cache, this populates
    //    the in-memory MessageManager and fires `on_change`,
    //    so per-page `reRender` listeners paint the cached
    //    state on the first frame. No network call.
    // 2. On a cache miss (first page load this tab, after a
    //    Logout/login cycle that cleared the cache, or after
    //    a Testing-page Refresh that wiped it), call
    //    `window._seed()` to do the network backfill. That
    //    populates the buffer, fires `on_change` at the end
    //    (which writes the new cache), done.
    await waitForPyDone(30000);
    let hydrated = false;
    if (typeof window._hydrate_from_cache === "function") {
      try {
        hydrated = await window._hydrate_from_cache();
      } catch (e) {
        console.warn("hydrate_from_cache failed:", e);
      }
    }
    if (!hydrated && typeof window._seed === "function") {
      try {
        await window._seed();
      } catch (e) {
        console.warn("seed failed:", e);
      }
    } else if (!hydrated) {
      console.warn("neither _hydrate_from_cache nor _seed ever appeared; skipping in-browser bootstrap");
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
