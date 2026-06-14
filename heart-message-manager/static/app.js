// Base-template JS bootstrap for every admin page.
//
// Loaded from `templates/base.html` (gated by `@login_required`).
// Reads `window.APP_CONFIG` (inlined server-side from `settings.toml`)
// and wires the shared in-browser state:
//   - `MessageBufferStore` (IndexedDB) for persistence
//   - `MqttWsClient` (MQTT-over-WebSocket) for live envelope delivery
//   - `MessageManager` (PyScript) for dispatch / ring buffer / config
//
// Lifecycle (per the design's wipe+re-seed triggers):
//   - App start:    wipe + re-seed from /api/messages + /api/config
//   - Login:        wipe + re-seed
//   - Long disconn: wipe + re-seed on reconnect (wasLongDisconnect flag)
//   - Per-page nav: hydrate from existing IndexedDB (no wipe)
//
// Connection status surfaces to a per-page #mqtt-status element
// (Live / Reconnecting / Paused / Error).
//
// Per-page scripts (e.g. the /preview page's PyScript init, the
// /messages listing refresh) call `window.App.registerOnMessageCallback(...)`
// to subscribe to the shared MessageManager.

(function () {
  "use strict";

  const SESSION_MARKER_KEY = "lindsay50_session_started";
  const SESSION_MARKER_VERSION = "v1";

  function setStatus(state, detail) {
    const el = document.getElementById("mqtt-status");
    if (!el) return;
    el.classList.remove(
      "bg-emerald-100",
      "text-emerald-700",
      "bg-amber-100",
      "text-amber-700",
      "bg-rose-100",
      "text-rose-700",
      "bg-slate-100",
      "text-slate-600"
    );
    let label = "Connecting…";
    let classes = ["bg-slate-100", "text-slate-600"];
    if (state === "connected") {
      label = "Live";
      classes = ["bg-emerald-100", "text-emerald-700"];
    } else if (state === "reconnecting") {
      const attempt = (detail && detail.attempt) || 0;
      const delay = (detail && detail.delayMs) || 0;
      label = `Reconnecting… (attempt ${attempt})`;
      classes = ["bg-amber-100", "text-amber-700"];
      void delay;
    } else if (state === "paused") {
      const elapsed = (detail && detail.elapsedMs) || 0;
      const secs = Math.floor(elapsed / 1000);
      label = `Paused (${secs}s elapsed)`;
      classes = ["bg-rose-100", "text-rose-700"];
    } else if (state === "error") {
      label = "Error";
      classes = ["bg-rose-100", "text-rose-700"];
    }
    el.textContent = label;
    el.classList.add(...classes);
  }

  function setPersistenceStatus(ok, error) {
    const el = document.getElementById("persistence-status");
    if (!el) return;
    if (ok) {
      el.classList.add("hidden");
    } else {
      el.classList.remove("hidden");
      el.textContent = "Persistence unavailable" + (error ? ` (${error})` : "");
    }
  }

  function getConfig() {
    return (typeof window.APP_CONFIG === "object" && window.APP_CONFIG) || {};
  }

  function hasSessionMarker() {
    try {
      return (
        localStorage.getItem(SESSION_MARKER_KEY) === SESSION_MARKER_VERSION
      );
    } catch (e) {
      return false;
    }
  }

  function setSessionMarker() {
    try {
      localStorage.setItem(SESSION_MARKER_KEY, SESSION_MARKER_VERSION);
    } catch (e) {
      // localStorage blocked — fall through, the per-page IndexedDB
      // hydrate will still re-populate from the REST API on first load.
    }
  }

  function clearSessionMarker() {
    try {
      localStorage.removeItem(SESSION_MARKER_KEY);
    } catch (e) {
      // ignore
    }
  }

  // Detect login (auth blueprint sets `data-wipe-on-load="true"` on body
  // immediately after a successful login redirect) and clear the marker.
  function maybeLoginWipe() {
    const body = document.body;
    if (body && body.getAttribute("data-wipe-on-load") === "true") {
      clearSessionMarker();
      body.removeAttribute("data-wipe-on-load");
    }
  }

  function getInitialWipeNeeded() {
    return !hasSessionMarker();
  }

  // Module-scope state — exposed via window.App
  let messageBufferStore = null;
  let mqttWsClient = null;
  let messageManager = null;
  let onMessageCallbacks = [];
  let wipeInProgress = null;

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

  function getMessageManager() {
    return messageManager;
  }

  function getMqttWsClient() {
    return mqttWsClient;
  }

  function getMessageBufferStore() {
    return messageBufferStore;
  }

  async function wipeAndReseed() {
    if (wipeInProgress) {
      // De-duplicate concurrent wipes.
      return wipeInProgress;
    }
    const cfg = getConfig();
    wipeInProgress = (async () => {
      // Wait for PyScript to expose window.messageManager (set by
      // preview.html's PyScript init). On non-PyScript pages the
      // manager is constructed lazily when a per-page script asks
      // for it. For the boot path, we expose a small helper at the
      // bottom of this file that constructs the manager from
      // APP_CONFIG + createMqttWsClient + createMessageBufferStore.
      if (messageBufferStore) {
        try {
          await messageBufferStore.wipe();
        } catch (e) {
          console.warn("IndexedDB wipe failed:", e);
        }
      }
      if (messageManager && typeof messageManager.seed === "function") {
        try {
          await messageManager.seed();
        } catch (e) {
          console.warn("Re-seed failed:", e);
        }
      }
      setSessionMarker();
    })();
    try {
      await wipeInProgress;
    } finally {
      wipeInProgress = null;
    }
    void cfg;
  }

  async function onMqttStatus(state, detail) {
    setStatus(state, detail);
    if (state === "connected" && detail && detail.wasLongDisconnect) {
      // Long disconnect window ended — wipe + re-seed.
      await wipeAndReseed();
    }
  }

  async function onMqttEnvelope(raw) {
    if (messageManager && typeof messageManager.dispatch === "function") {
      try {
        messageManager.dispatch(raw);
      } catch (e) {
        console.error("dispatch failed:", e);
      }
    }
  }

  async function startMqtt() {
    const cfg = getConfig();
    if (!cfg.mqttWsUrl) {
      setStatus("error", { error: "no MQTT_WS_URL in APP_CONFIG" });
      return;
    }
    // Lazy-import the JS shim via the static asset URL.
    // (The actual shim is loaded by PyScript on the /preview page; for
    // non-PyScript pages, we still want the connection — the in-browser
    // MessageManager registers itself for callbacks. The MqttWsClient
    // class is only used by the /preview page's PyScript init; the base
    // template just needs the WS + dispatch. We start the connection
    // here using the shim directly via dynamic import.)
    try {
      const mod = await import("./mqtt_ws_client.js");
      mqttWsClient = mod.createMqttWsClient({
        url: cfg.mqttWsUrl,
        username: cfg.mqttUsername,
        password: cfg.mqttPassword,
        topic: cfg.mqttTopic,
        longDisconnectMs: cfg.mqttLongDisconnectMs || 300000,
        onEnvelope: onMqttEnvelope,
        onStatus: onMqttStatus,
      });
      mqttWsClient.start();
    } catch (e) {
      console.error("Failed to start MQTT-WS client:", e);
      setStatus("error", { error: String(e) });
    }
  }

  async function init() {
    maybeLoginWipe();
    // Initialize IndexedDB store (the shim is loaded by the page's
    // own script tag — see base.html — so it's available globally).
    if (typeof createMessageBufferStore === "function") {
      messageBufferStore = createMessageBufferStore({
        dbName: "lindsay-50-browser",
      });
    } else {
      // Fallback: dynamic import for non-PyScript pages.
      try {
        const mod = await import("./message_buffer_store.js");
        messageBufferStore = mod.createMessageBufferStore({
          dbName: "lindsay-50-browser",
        });
      } catch (e) {
        console.warn("IndexedDB shim unavailable:", e);
        setPersistenceStatus(false, "shim missing");
      }
    }
    // On every page load, hydrate the in-memory state from IndexedDB
    // (no wipe on per-page navigation). The wipe only happens on
    // app start (no session marker) or login or long-disconnect.
    const wipeNeeded = getInitialWipeNeeded();
    // Wipe + re-seed from REST on first load (and on login via the
    // body attribute). Per-page navigation skips the wipe.
    if (wipeNeeded) {
      await wipeAndReseed();
    } else {
      // Hydrate from IndexedDB (per-page navigation)
      if (messageBufferStore && messageManager) {
        try {
          const [messages, config] = await messageBufferStore.hydrate();
          if (messageManager && typeof messageManager.hydrateFromStore === "function") {
            messageManager.hydrateFromStore(messages, config);
          }
        } catch (e) {
          console.warn("Hydrate failed:", e);
        }
      }
    }
    // Open the WS connection on every page (the buffer is current
    // across the whole app, not just /preview).
    await startMqtt();
  }

  // expose the registration surface for per-page scripts
  window.App = {
    registerOnMessageCallback,
    getMessageManager,
    getMqttWsClient,
    getMessageBufferStore,
    // Allow per-page scripts (e.g. /preview's PyScript init) to
    // install the shared MessageManager once PyScript is ready.
    setMessageManager: function (mgr) {
      messageManager = mgr;
    },
    // Allow a per-page wipe+re-seed trigger (used on login or on
    // detection of a stale session).
    wipeAndReseed,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
