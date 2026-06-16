// Base-template JS bootstrap for every admin page.
//
// Loaded from `templates/base.html` (gated by `@login_required`).
// Reads `window.APP_CONFIG` (inlined server-side from `settings.toml`)
// and seeds the in-browser state:
//
//   - On page load, if the PyScript-side `window._message_manager` is
//     installed (the PyScript runtime is loaded), call
//     `await window._seed()` — that fetches the message ring buffer
//     and the SignConfig from the Flask REST API and writes them
//     into the in-memory MessageManager. The seed runs once per
//     page load; SPA navigations within the page load don't re-fire
//     DOMContentLoaded, so the in-memory state stays current for
//     the lifetime of the page. The trigger is "auth-aware" because
//     `app.js` only loads when `current_user.is_authenticated` is
//     true — every login and every full-page-reload fires it.
//
//   - `window.App.registerOnMessageCallback(cb)` lets per-page
//     scripts (e.g. /preview's PyScript init, the /testing page's
//     message feed) subscribe to inbound MQTT envelopes. The
//     `dispatchToCallbacks` fan-out is fed by the PyScript-side
//     MessageManager (`app_main.py` wires MqttWsClient →
//     MessageManager.dispatch → on_message → window.App._dispatchToCallbacks).
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
// call `window.App.registerOnMessageCallback(...)` to subscribe
// to inbound envelopes and `window.App.getMessages(...)` /
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

  // Per-page callback registration. The PyScript-side
  // `app_main.py._on_message_js` calls `App._dispatchToCallbacks`
  // on every inbound message envelope; the registered callbacks
  // here fan that out to per-page listeners.
  const onMessageCallbacks = [];

  function registerOnMessageCallback(cb) {
    if (typeof cb === "function") {
      onMessageCallbacks.push(cb);
    }
  }

  function dispatchToCallbacks(msg) {
    for (const cb of onMessageCallbacks) {
      try {
        cb(msg);
      } catch (e) {
        console.error("onMessage callback error:", e);
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

  async function init() {
    // Single seed trigger — runs once per page load (gated by
    // `@login_required` on every admin route, so this only runs
    // for authenticated users). Covers login (which always
    // redirects to a fresh page) and full-page-reload (Pi-reboot
    // analog). The PyScript side does the actual seeding; PyScript
    // 2024.9.x loads asynchronously and may not have installed
    // `window._seed` by the time `DOMContentLoaded` fires, so we
    // poll for it (cap ~5s) before falling back. The 5s window
    // covers PyScript's normal bootstrap on a cold load (micropip
    // + numpy + Pillow + the in-browser render path) — a slow
    // load surfaces as a "No messages" placeholder that fills in
    // when the seed resolves, not as a permanently-empty feed.
    const seedDeadline = Date.now() + 5000;
    while (typeof window._seed !== "function" && Date.now() < seedDeadline) {
      await new Promise((r) => setTimeout(r, 50));
    }
    if (typeof window._seed === "function") {
      try {
        await window._seed();
      } catch (e) {
        console.warn("seed failed:", e);
      }
    } else {
      console.warn("window._seed never appeared; skipping in-browser seed");
    }
  }

  // expose the registration surface for per-page scripts.
  // `getMessages` / `getConfig` are JS-side wrappers that delegate
  // to the PyScript-side MessageManager; `_dispatchToCallbacks` is
  // what `app_main.py` calls on each inbound envelope to fan out
  // to the registered per-page callbacks.
  window.App = {
    registerOnMessageCallback,
    getMessages,
    getConfig: getConfigNow,
    // PyScript-side fan-out entry point. The MessageManager's
    // on_message callback calls this; per-page listeners
    // (e.g. preview.js's callback) are reached via
    // `dispatchToCallbacks`.
    _dispatchToCallbacks: dispatchToCallbacks,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
