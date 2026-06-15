// Base-template JS bootstrap for every admin page.
//
// Loaded from `templates/base.html` (gated by `@login_required`).
// Reads `window.APP_CONFIG` (inlined server-side from `settings.toml`)
// and wires the shared in-browser state:
//   - `MessageBufferStore` (IndexedDB) for persistence
//   - `MqttWsClient` (MQTT-over-WebSocket) for live envelope delivery
//   - `MessageManager` (PyScript, on /preview) for the in-memory mirror
//
// The single source of truth for the in-browser ring buffer lives in
// IndexedDB. Every inbound MQTT envelope is parsed, persisted (under
// the same `messages` / `config` object stores the Python wrapper
// uses), and then dispatched to per-page callbacks. The same filter
// + enrichment logic the Python `FilteredMessages` applies is mirrored
// here so non-PyScript pages (e.g. /testing) can render the same
// "suppressed / visible / rules" view without spinning up a Python
// runtime.
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
// /testing page's message feed) call `window.App.registerOnMessageCallback(...)`
// to subscribe to inbound envelopes and `window.App.getMessages(...)` /
// `getConfig()` to read the current buffer.

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

  // Detect login: auth.py appends `?wipe=1` to the post-login redirect
  // URL. When present, the browser should wipe the IndexedDB message
  // buffer and re-seed from REST (the previous session's data may
  // belong to a different user, and the in-memory MessageManager
  // mirrored to it). The query param is removed from the URL after
  // handling so a reload doesn't re-trigger the wipe.
  function consumeLoginWipeParam() {
    try {
      const url = new URL(window.location.href);
      if (url.searchParams.get("wipe") === "1") {
        url.searchParams.delete("wipe");
        // Use replaceState so the wipe=1 doesn't end up in browser history.
        window.history.replaceState({}, "", url.toString());
        return true;
      }
    } catch (e) {
      // ignore — fall through, no wipe
    }
    return false;
  }

  function getInitialWipeNeeded() {
    return !hasSessionMarker();
  }

  // ---------------------------------------------------------------------------
  // Filter / enrichment — JS mirror of lib_shared/messages.F filtered_messages.
  // Kept intentionally small and self-contained. The Python side has the
  // authoritative version; the two should produce the same suppressed / rules
  // result for the same config + messages.
  // ---------------------------------------------------------------------------

  function _matchesFilterRule(msg, rule) {
    if (!rule || rule.action !== "suppress") return false;
    const body = (msg.body || "").toString();
    if (rule.type === "keyword") {
      return body.toLowerCase().includes((rule.pattern || "").toLowerCase());
    } else if (rule.type === "regex") {
      try {
        return new RegExp("^(?:" + rule.pattern + ")$").test(body);
      } catch (e) {
        return false;
      }
    } else if (rule.type === "sender") {
      return msg.sender === rule.pattern;
    } else if (rule.type === "message") {
      return msg.id === rule.pattern;
    }
    return false;
  }

  function _applyFilters(msg, rules) {
    const suppressing = [];
    for (const rule of rules || []) {
      if (_matchesFilterRule(msg, rule)) suppressing.push(rule);
    }
    return suppressing;
  }

  function _formatDisplayTime(receivedAt, tzOffsetMins) {
    if (!receivedAt) return "";
    // receivedAt is ISO 8601 UTC ("2026-05-22T14:30:00Z"). Apply the
    // sign's signed UTC offset (in minutes) without depending on the
    // browser's local timezone.
    const iso = receivedAt.replace(/Z$/, "");
    const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})/.exec(iso);
    if (!m) return receivedAt;
    const utcEpoch = Date.UTC(
      parseInt(m[1], 10),
      parseInt(m[2], 10) - 1,
      parseInt(m[3], 10),
      parseInt(m[4], 10),
      parseInt(m[5], 10),
      parseInt(m[6], 10)
    );
    const localEpoch = utcEpoch + (tzOffsetMins || 0) * 60 * 1000;
    const d = new Date(localEpoch);
    const monthAbbr = [
      "Jan", "Feb", "Mar", "Apr", "May", "Jun",
      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ][d.getUTCMonth()];
    const day = d.getUTCDate();
    let hour = d.getUTCHours();
    const minute = d.getUTCMinutes();
    const ampm = hour >= 12 ? "PM" : "AM";
    hour = hour % 12;
    if (hour === 0) hour = 12;
    const mm = minute < 10 ? "0" + minute : "" + minute;
    return monthAbbr + " " + day + " " + hour + ":" + mm + " " + ampm;
  }

  // Enrich a flat message dict with `source`, `suppressed`, `rules`,
  // `sender_name`, `display_time` — same shape `FilteredMessages._enrich_messages`
  // produces on the Python side.
  function _enrichMessage(msg, config, source) {
    const rules = _applyFilters(msg, config && config.filters);
    const sendersList = (config && config.senders) || [];
    const sendersByPhone = {};
    for (const s of sendersList) {
      if (s && s.phone) sendersByPhone[s.phone] = s.name;
    }
    return Object.assign({}, msg, {
      source: source,
      suppressed: rules.length > 0,
      rules: rules,
      sender_name: sendersByPhone[msg.sender] || "",
      display_time: _formatDisplayTime(
        msg.received_at,
        (config && config.tz_offset_mins) || 0
      ),
    });
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

  // Public: read messages from the IndexedDB ring buffer, with the same
  // suppression / display-time enrichment the Python side applies. Used
  // by non-PyScript pages (e.g. /testing) to render the feed.
  async function getMessages(limit, suppress) {
    if (suppress === undefined) suppress = true;
    if (!messageBufferStore) return [];
    try {
      const data = await messageBufferStore.hydrate();
      const messages = (data && data.messages) || [];
      const config = (data && data.config) || {};
      const enriched = messages.map((m) =>
        _enrichMessage(m, config, m.source || "rest")
      );
      const filtered = suppress ? enriched.filter((e) => !e.suppressed) : enriched;
      filtered.sort((a, b) => (a.received_at > b.received_at ? -1 : 1));
      return limit ? filtered.slice(0, limit) : filtered;
    } catch (e) {
      console.warn("getMessages failed:", e);
      return [];
    }
  }

  // Public: read the current config from IndexedDB (or null).
  async function getConfigNow() {
    if (!messageBufferStore) return null;
    try {
      const data = await messageBufferStore.hydrate();
      return (data && data.config) || null;
    } catch (e) {
      console.warn("getConfig failed:", e);
      return null;
    }
  }

  // Fetch /api/messages with X-API-Key. Returns a list of Message dicts.
  // Retries once on network error — fetches fired during page load are
  // sometimes cancelled by the browser (Chrome logs these as
  // net::ERR_ABORTED with no HTTP status). A short backoff gives the
  // page a chance to finish loading before retrying.
  async function _fetchMessagesFromApi(apiUrl, apiKey) {
    if (!apiUrl) return [];
    const attempt = async () => {
      const res = await fetch(apiUrl, {
        method: "GET",
        headers: { "X-API-Key": apiKey || "" },
      });
      if (!res.ok) {
        throw new Error("GET " + apiUrl + " -> HTTP " + res.status);
      }
      return await res.json();
    };
    try {
      return await attempt();
    } catch (e) {
      if (e instanceof TypeError && /Failed to fetch|NetworkError/i.test(e.message)) {
        await new Promise((r) => setTimeout(r, 250));
        return await attempt();
      }
      throw e;
    }
  }

  // Fetch /api/config with X-API-Key. Returns a SignConfig dict. Same
  // retry policy as `_fetchMessagesFromApi`.
  async function _fetchConfigFromApi(apiUrl, apiKey) {
    if (!apiUrl) return null;
    const attempt = async () => {
      const res = await fetch(apiUrl, {
        method: "GET",
        headers: { "X-API-Key": apiKey || "" },
      });
      if (!res.ok) {
        throw new Error("GET " + apiUrl + " -> HTTP " + res.status);
      }
      return await res.json();
    };
    try {
      return await attempt();
    } catch (e) {
      if (e instanceof TypeError && /Failed to fetch|NetworkError/i.test(e.message)) {
        await new Promise((r) => setTimeout(r, 250));
        return await attempt();
      }
      throw e;
    }
  }

  // Persist a list of messages (Message.to_dict() shape) into IndexedDB.
  async function _seedMessages(messages) {
    if (!messageBufferStore) return;
    for (const m of messages) {
      try {
        await messageBufferStore.putMessage(
          Object.assign({ source: "rest" }, m)
        );
      } catch (e) {
        // Non-fatal — IndexedDB quota / transient error.
      }
    }
  }

  async function wipeAndReseed() {
    if (wipeInProgress) {
      // De-duplicate concurrent wipes.
      return wipeInProgress;
    }
    wipeInProgress = (async () => {
      const cfg = getConfig();
      if (messageBufferStore) {
        try {
          await messageBufferStore.wipe();
        } catch (e) {
          console.warn("IndexedDB wipe failed:", e);
        }
      }
      // PyScript pages: ask the in-memory MessageManager to re-seed
      // (the manager owns its own state and will write through to the
      // store via putMessage / putConfig).
      if (messageManager && typeof messageManager.seed === "function") {
        try {
          await messageManager.seed();
        } catch (e) {
          console.warn("Re-seed failed:", e);
        }
      } else if (messageBufferStore) {
        // Non-PyScript pages: seed IndexedDB directly from the REST
        // API. This is the path /testing and other plain-JS pages use.
        let seededConfig = null;
        try {
          const msgs = await _fetchMessagesFromApi(
            cfg.messagesApiUrl,
            cfg.apiKey
          );
          if (Array.isArray(msgs)) {
            await _seedMessages(msgs);
          }
        } catch (e) {
          console.warn("Message seed from REST failed:", e);
        }
        try {
          const cfgDict = await _fetchConfigFromApi(
            cfg.configApiUrl,
            cfg.apiKey
          );
          if (cfgDict) {
            await messageBufferStore.putConfig(cfgDict);
            seededConfig = cfgDict;
          }
        } catch (e) {
          console.warn("Config seed from REST failed:", e);
        }
        // Fire a synthetic "config" envelope so per-page callbacks
        // (e.g. testing.html's renderConfig) re-render after the
        // initial seed completes. Without this, the first
        // renderMessages() can run against an empty store mid-seed and
        // miss the populated state.
        //
        // Always dispatch, even if the config fetch failed — the
        // per-page callback handler re-renders messages regardless of
        // the dispatch payload's shape, so a null/empty seed still
        // triggers the feed to re-read IndexedDB (which by then has
        // the seeded messages). Gating on `seededConfig` here would
        // leave the testing feed stuck on the initial empty render
        // until the next MQTT envelope arrives.
        dispatchToCallbacks(seededConfig);
      }
      setSessionMarker();
    })();
    try {
      await wipeInProgress;
    } finally {
      wipeInProgress = null;
    }
  }

  async function onMqttStatus(state, detail) {
    setStatus(state, detail);
    console.info("[mqtt-ws]", state, detail || {});
    if (state === "connected" && detail && detail.wasLongDisconnect) {
      // Long disconnect window ended — wipe + re-seed.
      await wipeAndReseed();
    }
  }

  async function onMqttEnvelope(raw) {
    // The envelope is JSON: { "type": "message" | "config", "payload": ... }.
    // Persist to IndexedDB, then fire per-page callbacks. Both branches
    // share the same store so a PyScript page and a non-PyScript page
    // see the same data.
    let envelope;
    try {
      envelope = JSON.parse(raw);
    } catch (e) {
      console.warn("Invalid MQTT envelope (not JSON):", e);
      return;
    }
    if (!envelope || typeof envelope !== "object") return;

    // Mirror to the in-memory MessageManager (if /preview registered one).
    if (messageManager && typeof messageManager.dispatch === "function") {
      try {
        messageManager.dispatch(raw);
      } catch (e) {
        console.error("dispatch failed:", e);
      }
    }

    // Persist to IndexedDB.
    if (messageBufferStore) {
      try {
        if (envelope.type === "message" && envelope.payload) {
          await messageBufferStore.putMessage(
            Object.assign({ source: "mqtt" }, envelope.payload)
          );
        } else if (envelope.type === "config" && envelope.payload) {
          await messageBufferStore.putConfig(envelope.payload);
        }
      } catch (e) {
        console.warn("IndexedDB persist failed:", e);
      }
    }

    // Fire per-page callbacks with the raw payload. The Python on_message
    // callback signature is `Message`; the JS-side equivalent is the
    // payload dict (which is `Message.to_dict()` shape — flat fields).
    dispatchToCallbacks(envelope.payload || envelope);
  }

  async function startMqtt() {
    const cfg = getConfig();
    if (!cfg.mqttWsUrl) {
      setStatus("error", { error: "no MQTT_WS_URL in APP_CONFIG" });
      return;
    }
    // Resolve the JS shim against the document origin, not the page's
    // URL — `import("./...")` uses the document base URL, so a relative
    // path resolves to `/<page>/mqtt_ws_client.js` (next to the current
    // route) rather than `/static/mqtt_ws_client.js`. Hardcode the
    // Flask static path; the server always serves it there.
    // The `?v=7` matches base.html — bump together when the shim changes
    // so a stale browser cache can't pin to a broken encode/decode path.
    const mqttWsUrl =
      window.location.origin + "/static/mqtt_ws_client.js?v=7";
    try {
      const mod = await import(mqttWsUrl);
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
    // Wipe + re-seed from REST on first load (no session marker) and
    // immediately after a fresh login (the auth blueprint appends
    // ?wipe=1 to the post-login redirect URL). Subsequent reloads
    // while the session is still valid skip the wipe — the buffer is
    // current.
    const loginWipe = consumeLoginWipeParam();
    // Initialize IndexedDB store (the shim is loaded by the page's
    // own script tag — see base.html — so it's available globally).
    if (typeof createMessageBufferStore === "function") {
      messageBufferStore = createMessageBufferStore({
        dbName: "lindsay-50-browser",
      });
    } else {
      // Fallback: dynamic import for non-PyScript pages. Resolve
      // against the document origin (the shim is served by Flask at
      // /static/, not at the page's URL).
      try {
        const mod = await import(
          window.location.origin + "/static/message_buffer_store.js"
        );
        messageBufferStore = mod.createMessageBufferStore({
          dbName: "lindsay-50-browser",
        });
      } catch (e) {
        console.warn("IndexedDB shim unavailable:", e);
        setPersistenceStatus(false, "shim missing");
      }
    }
    // Wipe + re-seed from REST on first load (no session marker) and
    // immediately after a fresh login. Per-page navigation skips the
    // wipe.
    const wipeNeeded = loginWipe || getInitialWipeNeeded();
    if (wipeNeeded) {
      await wipeAndReseed();
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
    // Read APIs for non-PyScript pages.
    getMessages,
    getConfig: getConfigNow,
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
