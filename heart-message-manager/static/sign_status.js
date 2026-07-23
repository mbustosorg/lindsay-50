// sign_status.js — Dashboard pill + Settings-page Sign Health section.
//
// One in-memory `latestSnapshot` (most recent accepted payload), fed by:
//   1. A single load-time `fetch('/api/sign-status')` (one-shot, NOT a timer).
//      Last-write-wins by `updated_at` — a stale load-time response cannot
//      overwrite a fresher WS message (Decision 7 in openspec/changes/
//      add-sign-status-reports/design.md).
//   2. A second `createMqttWsClient` instance for MQTT_STATUS_TOPIC —
//      each decoded message with valid required keys replaces the snapshot
//      and triggers a re-render. Malformed/missing-keys payloads are
//      logged at WARN via console.warn and dropped (browser mirrors the
//      Flask-side rejection in lib_shared.sign_status).
//
// State (freshness) is computed from `(now - snapshot.updated_at)` and
// falls into one of three buckets (live / unknown / offline). Health is
// computed from the snapshot's contents (mqtt_connected / last_error).
// Both signals combine to drive a 4-state pill render.
//
// The module is short-circuited (no DOM writes, no WS, no fetch) when
// neither #sign-live-pill (dashboard) nor [data-sign-status-field]
// (settings) is present — so it's safe to include on every page via
// base.html (Decision 4.3).
//
// State-machine timer: a 5s setInterval re-evaluates state AND health
// and re-renders the DOM. The interval fires regardless of network
// state and produces only DOM updates — NO fetch, NO new WS
// (Decision 6 + 13 in design.md).

const LIVE_THRESHOLD_S = 15;
const UNKNOWN_THRESHOLD_S = 30;
const RERENDER_INTERVAL_MS = 5000;

// REQUIRED_SNAPSHOT_KEYS mirrors lib_shared.sign_status.REQUIRED_SNAPSHOT_KEYS —
// both ends validate against the same 8-key set so the wire shape and
// the docs stay in lockstep. If this list changes on the Flask side,
// mirror it here.
const REQUIRED_SNAPSHOT_KEYS = [
  "schema_version",
  "active_sha",
  "short_sha",
  "started_at",
  "updated_at",
  "uptime_seconds",
  "mqtt_connected",
  "last_error",
];

function isValidSnapshot(obj) {
  if (!obj || typeof obj !== "object") return false;
  for (const key of REQUIRED_SNAPSHOT_KEYS) {
    if (!(key in obj)) return false;
  }
  return true;
}

// `stateFromAge(ageSeconds)` — browser-side freshness policy.
// Three states. Thresholds tuned to the 5s publish cadence: a 15s window
// catches 3 missed publishes (the "something is wrong" signal); 30s
// catches 6 (the "definitively unreachable" signal).
function stateFromAge(ageSeconds) {
  if (ageSeconds === null || Number.isNaN(ageSeconds)) return "offline";
  if (ageSeconds < LIVE_THRESHOLD_S) return "live";
  if (ageSeconds < UNKNOWN_THRESHOLD_S) return "unknown";
  return "offline";
}

// `healthFromSnapshot(snapshot)` — browser-side content policy.
// Two signals: mqtt_connected must be true; last_error must be null
// or empty. `last_tick_age_ms` was dropped from the snapshot — the
// `_LAST_TICK_MONOTONIC` bookkeeping that would have produced a real
// value was never wired up, so the field always read 0 and the
// threshold was vacuous.
function healthFromSnapshot(snapshot) {
  if (!snapshot) return "healthy"; // default — no signal = assume OK
  if (snapshot.mqtt_connected === false) return "degraded";
  const err = snapshot.last_error;
  if (typeof err === "string" && err.length > 0) return "degraded";
  return "healthy";
}

// Render the Dashboard pill as a function of (state, health).
// Four render states, matching Decision 11 in design.md:
//   live + healthy  → green + pulse + "Live"
//   live + degraded → amber + no-pulse + "Degraded"
//   unknown         → amber + no-pulse + "Unknown"
//   offline         → grey + no-pulse + "Offline"
// The "unknown + degraded" row collapses to "Unknown — Degraded" because
// in practice an unknown message is already a degraded state.
function combinedState(snapshot, now) {
  const state = computeState(snapshot, now);
  const health = healthFromSnapshot(snapshot);
  let renderKey;
  let text;
  let classes;
  if (state === "live" && health === "healthy") {
    renderKey = "live-healthy";
    text = "Live";
    classes = "px-3 py-1.5 rounded-full bg-green-100 text-green-700 text-xs font-semibold flex items-center gap-2";
  } else if (state === "live" && health === "degraded") {
    renderKey = "live-degraded";
    text = "Degraded";
    classes = "px-3 py-1.5 rounded-full bg-amber-100 text-amber-800 text-xs font-semibold flex items-center gap-2";
  } else if (state === "unknown") {
    renderKey = health === "degraded" ? "unknown-degraded" : "unknown-healthy";
    text = health === "degraded" ? "Unknown — Degraded" : "Unknown";
    classes = "px-3 py-1.5 rounded-full bg-amber-100 text-amber-800 text-xs font-semibold flex items-center gap-2";
  } else {
    renderKey = "offline";
    text = "Offline";
    classes = "px-3 py-1.5 rounded-full bg-slate-100 text-slate-600 text-xs font-semibold flex items-center gap-2";
  }
  // The inner dot matches the pill color and animates only when Live.
  const dotColor = renderKey === "live-healthy"
    ? "w-2 h-2 bg-green-500 rounded-full animate-pulse"
    : renderKey === "live-degraded" || renderKey === "unknown-degraded" || renderKey === "unknown-healthy"
    ? "w-2 h-2 bg-amber-500 rounded-full"
    : "w-2 h-2 bg-slate-400 rounded-full";
  return { state, health, renderKey, text, classes, dotColor };
}

function computeState(snapshot, now) {
  if (!snapshot || !snapshot.updated_at) return "offline";
  const t = Date.parse(snapshot.updated_at);
  if (Number.isNaN(t)) return "offline";
  const ageSeconds = (now - t) / 1000;
  return stateFromAge(ageSeconds);
}

// `formatUptime(seconds)` — `Xd Yh Zm`. Falls back gracefully on
// negative or non-numeric values.
function formatUptime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "—";
  const totalMinutes = Math.floor(seconds / 60);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes - days * 60 * 24) / 60);
  const minutes = totalMinutes - days * 60 * 24 - hours * 60;
  const parts = [];
  if (days > 0) parts.push(`${days}d`);
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0 || parts.length === 0) parts.push(`${minutes}m`);
  return parts.join(" ");
}

// `formatBrowserTimestamp(ms)` — local time `HH:MM:SS`.
function formatBrowserTimestamp(ms) {
  if (!Number.isFinite(ms)) return "—";
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// -----------------------------------------------------------------------------
// Rendering — the four state × health combinations
// -----------------------------------------------------------------------------

function applyPillRender(rendered) {
  const pill = document.getElementById("sign-live-pill");
  if (!pill) return;
  pill.dataset.state = rendered.renderKey;
  pill.className = rendered.classes;
  // Inner markup: dot + text. Re-create the dot's class so the
  // pulse animation toggles correctly on state transitions.
  const dot = document.createElement("span");
  dot.className = rendered.dotColor;
  pill.replaceChildren(dot, document.createTextNode(rendered.text));
}

function applyFieldsRender(snapshot, rendered) {
  // The Settings-page slots use [data-sign-status-field="<name>"] for
  // individual values. The whole field container is wrapped in
  // [data-sign-status-fields]; the placeholder is [data-sign-status-placeholder].
  const fieldsContainer = document.querySelector("[data-sign-status-fields]");
  const placeholder = document.querySelector("[data-sign-status-placeholder]");
  const degradedBanner = document.querySelector("[data-sign-status-degraded-banner]");
  if (!fieldsContainer && !placeholder) return; // settings page not loaded

  if (rendered.state === "offline" || !snapshot) {
    if (fieldsContainer) fieldsContainer.style.display = "none";
    if (placeholder) placeholder.style.display = "";
    if (degradedBanner) degradedBanner.style.display = "none";
    return;
  }

  if (fieldsContainer) fieldsContainer.style.display = "";
  if (placeholder) placeholder.style.display = "none";

  // Populate field slots in place. The browser's clock is the receipt
  // time of the WS message or load-time fetch.
  const now = Date.now();
  const populated = {
    active_sha: snapshot.active_sha || "",
    short_sha: snapshot.short_sha || "",
    started_at: snapshot.started_at || "",
    uptime_seconds: formatUptime(snapshot.uptime_seconds),
    mqtt_connected: snapshot.mqtt_connected ? "true" : "false",
    last_error: snapshot.last_error && snapshot.last_error.length
      ? snapshot.last_error
      : "—",
    received_at_browser: formatBrowserTimestamp(snapshot._receivedAtMs || now),
  };
  for (const [key, val] of Object.entries(populated)) {
    const el = document.querySelector(`[data-sign-status-field="${key}"]`);
    if (el) el.textContent = String(val);
  }

  if (degradedBanner) {
    if (rendered.health === "degraded") {
      let reason = "";
      if (snapshot.mqtt_connected === false) reason = "MQTT disconnected";
      else if (snapshot.last_error && snapshot.last_error.length) {
        reason = `Last error: ${snapshot.last_error}`;
      }
      const textEl = degradedBanner.querySelector("[data-degraded-reason]");
      if (textEl && reason) textEl.textContent = reason;
      degradedBanner.style.display = "";
    } else {
      degradedBanner.style.display = "none";
    }
  }
}

function renderAll(snapshot) {
  const now = Date.now();
  const rendered = combinedState(snapshot, now);
  applyPillRender(rendered);
  applyFieldsRender(snapshot, rendered);
}

// -----------------------------------------------------------------------------
// Snapshot acceptance — last-write-wins by `updated_at`
// -----------------------------------------------------------------------------

let _latestSnapshot = null;
let _latestReceivedAt = 0; // browser's clock at moment of receipt

function maybeAcceptSnapshot(parsed) {
  if (!isValidSnapshot(parsed)) {
    console.warn(
      "[sign_status.js] dropping invalid snapshot (missing required keys):",
      parsed
    );
    return;
  }
  // Last-write-wins: only accept if newer than what we have.
  const incomingTs = Date.parse(parsed.updated_at);
  if (
    _latestSnapshot &&
    Number.isFinite(Date.parse(_latestSnapshot.updated_at)) &&
    Number.isFinite(incomingTs) &&
    incomingTs <= Date.parse(_latestSnapshot.updated_at)
  ) {
    return; // older or equal; ignore
  }
  parsed._receivedAtMs = Date.now();
  _latestSnapshot = parsed;
  _latestReceivedAt = Date.now();
  renderAll(_latestSnapshot);
}

// -----------------------------------------------------------------------------
// Load-time fetch (one-shot, NOT a timer — Decision 4 / 7)
// -----------------------------------------------------------------------------

async function hydrateFromServer() {
  try {
    const resp = await fetch("/api/sign-status", { cache: "no-store" });
    if (!resp.ok) {
      console.warn("[sign_status.js] /api/sign-status returned", resp.status);
      return;
    }
    const payload = await resp.json();
    if (payload && payload.snapshot && isValidSnapshot(payload.snapshot)) {
      // Only accept if not older than what WS already delivered.
      maybeAcceptSnapshot(payload.snapshot);
    }
    // `null` snapshot is expected (Flask hasn't received anything yet) —
    // do NOT replace the in-memory snapshot with null.
  } catch (e) {
    console.warn("[sign_status.js] hydrateFromServer failed:", e && e.message ? e.message : e);
  }
}

// -----------------------------------------------------------------------------
// Module init
// -----------------------------------------------------------------------------

let _rerenderTimer = null;
let _statusWsClient = null;

function pageHasStatusUi() {
  return Boolean(
    document.getElementById("sign-live-pill") ||
      document.querySelector("[data-sign-status-field]")
  );
}

function startRerenderTimer() {
  if (_rerenderTimer != null) return;
  // Local setInterval — does NOT poll the server. Re-renders the
  // pill from the in-memory snapshot every 5s so state transitions
  // (live → unknown → offline, healthy → degraded) are visible
  // even when no new message arrives.
  _rerenderTimer = setInterval(() => {
    renderAll(_latestSnapshot);
  }, RERENDER_INTERVAL_MS);
  window.addEventListener("beforeunload", () => {
    if (_rerenderTimer != null) {
      clearInterval(_rerenderTimer);
      _rerenderTimer = null;
    }
  });
}

function openStatusWs() {
  if (typeof window.createMqttWsClient !== "function") {
    console.warn(
      "[sign_status.js] window.createMqttWsClient not available; status flow disabled"
    );
    return;
  }
  const cfg = window.APP_CONFIG || {};
  const statusTopic = cfg.mqttStatusTopic || cfg.MQTT_STATUS_TOPIC || "";
  if (!statusTopic) {
    console.warn("[sign_status.js] no status topic in APP_CONFIG; status flow disabled");
    return;
  }
  _statusWsClient = window.createMqttWsClient({
    url: cfg.mqttWsUrl,
    username: cfg.mqttUsername || cfg.MQTT_USERNAME || "",
    password: cfg.mqttPassword || cfg.MQTT_PASSWORD || "",
    topic: statusTopic,
    onEnvelope: (rawString) => {
      let parsed = null;
      try {
        parsed = JSON.parse(rawString);
      } catch (e) {
        console.warn("[sign_status.js] status WS payload not JSON:", e && e.message ? e.message : e);
        return;
      }
      maybeAcceptSnapshot(parsed);
    },
    onStatus: (state, detail) => {
      // The WS connection state for the status topic is surfaced
      // via the preview's MQTT pill (see dashboard.html). The
      // page no longer renders a separate `#sign-status-ws-state`
      // indicator — the operator reads WS state from the same pill
      // that summarizes the envelope-feed connection, with the
      // WS URL + subscribe topic in the pill's tooltip.
      void detail;
    },
  });
  _statusWsClient.start();
}

function init() {
  if (!pageHasStatusUi()) {
    // Page has no pill or settings slots — no-op. Lets the script
    // be included globally via base.html without side effects.
    return;
  }
  // Render once with the empty snapshot so the placeholder text
  // ("Offline") shows before any fetch or WS resolves.
  renderAll(_latestSnapshot);
  hydrateFromServer();
  openStatusWs();
  startRerenderTimer();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
