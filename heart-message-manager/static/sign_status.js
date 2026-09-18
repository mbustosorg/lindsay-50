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
// both ends validate against the same 9-key set so the wire shape and
// the docs stay in lockstep. If this list changes on the Flask side,
// mirror it here. v2 / issue #71 adds `applied_config_sha`.
const REQUIRED_SNAPSHOT_KEYS = [
  "schema_version",
  "active_sha",
  "short_sha",
  "started_at",
  "updated_at",
  "uptime_seconds",
  "mqtt_connected",
  "last_error",
  "applied_config_sha",
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
  // Issue #71: also bail if there are no per-cell slots at all. The
  // Settings page (`templates/settings.html`) uses a single bare
  // `[data-sign-status-field="short_sha"]` cell with NO wrapper and
  // NO placeholder — the prior guard `!fieldsContainer && !placeholder`
  // would short-circuit before populating that cell. Always write to
  // any per-field cells that DO exist; the wrapper is purely a
  // show/hide toggle for the placeholder layout.
  const hasAnyFieldCell = document.querySelector("[data-sign-status-field]");
  if (!fieldsContainer && !placeholder && !hasAnyFieldCell) return;

  if (!snapshot) {
    if (fieldsContainer) fieldsContainer.style.display = "none";
    if (placeholder) placeholder.style.display = "";
    if (degradedBanner) degradedBanner.style.display = "none";
    // No live snapshot AND no persisted snapshot — the Pi cells will
    // stay "—" (server-rendered). Nothing to populate.
    return;
  }

  // NOTE: we no longer short-circuit the field-cell writes on
  // `rendered.state === "offline"`. When the snapshot is a
  // *persisted* one from `sign_status_log` (Flask restored it on
  // startup before the broker sent anything new), `updated_at` is
  // the timestamp of the Pi's LAST online status publish — which
  // is by definition older than 30s. `stateFromAge` flips to
  // "offline" or "unknown" depending on age, and the prior guard
  // blocked the per-cell writes — meaning a hard refresh before
  // the first live WS message would leave Pi/Code and Pi/Config
  // as "—" even though Flask has the Pi's last-known state.
  //
  // The state machine's job is the freshness pill ("Live /
  // Unknown / Offline"), not a gate on field-cell display. The
  // Pi cells ALWAYS populate from whatever snapshot we have
  // (live or persisted); the column-level drift comparison in
  // `applyVersionDriftRender` separately accounts for stale
  // Pi data via `liveByColumn`, so a "Pi was running X but is
  // offline" cell doesn't false-positive the Code/Config column
  // red rule.
  //
  // The placeholder-vs-fieldsContainer visibility toggle below is
  // the right gate for the "do we show the populated cells or the
  // placeholder?" question.

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
    // Issue #71: applied_config_sha is the SHA of the most-recent
    // config envelope the Pi applied to its in-memory SignConfig.
    // Empty string on cold start (no envelope yet) — the dashboard
    // renders that as "—" with no red highlight.
    applied_config_sha: snapshot.applied_config_sha || "",
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

// -----------------------------------------------------------------------------
// Versions card (issue #71)
// -----------------------------------------------------------------------------

// Cell labels — the keys the template emits in `data-version-drift-cell`
// and the per-column disagreement buckets. Two COLUMNS (code, config),
// three ROWS (flask, pi, browser). Each column has up to 3 values; if
// any pair of non-empty values disagrees, every populated cell in that
// column flips red. Empty ("") cells stay neutral — cold start is not
// drift.
const VERSION_DRIFT_COLUMNS = ["code", "config"];
const VERSION_DRIFT_ROWS = ["flask", "pi", "browser"];
const VERSION_DRIFT_DASH = "—";

function applyVersionDriftRender(snapshot) {
  const table = document.querySelector("[data-version-drift-table]");
  if (!table) return; // page doesn't host the card

  const cfg = window.APP_CONFIG || {};

  // Read the live cell values. The Flask row is static (server-rendered
  // into the cell text + data-attrs; the JS reads them so the comparison
  // is from the same source as the rendered text). Pi cells are populated
  // by `applyFieldsRender` above (writes to [data-sign-status-field]). The
  // Browser/Code cell is server-rendered to Flask's SHA by default; we
  // override with `window._loaded_python_short_sha` if the PyScript
  // runtime has reported a different value (caches may pin to an older
  // build). The Browser/Config cell is filled by
  // `applyBrowserConfigReceipt()` on every on_change fan-out.
  function readCell(row, col) {
    const el = table.querySelector(
      `[data-version-drift-cell="${row}-${col}"]`
    );
    if (!el) return "";
    let v = (el.textContent || "").trim();
    if (v === VERSION_DRIFT_DASH) v = "";
    return v;
  }

  // Override Browser/Code if PyScript reported a different loaded SHA.
  const browserCodeCell = table.querySelector(
    '[data-version-drift-cell="browser-code"]'
  );
  if (browserCodeCell && typeof window._loaded_python_short_sha === "string"
      && window._loaded_python_short_sha.length > 0) {
    browserCodeCell.textContent = window._loaded_python_short_sha;
  }

  const flaskCode = cfg.flaskVersion || readCell("flask", "code");
  // Flask/Config: prefer the live receipt cache (set by
  // `applyBrowserConfigReceipt` whenever a new config envelope lands),
  // fall back to the page-load value baked into APP_CONFIG, fall back
  // to whatever the cell currently shows. The receipt cache is the
  // post-save value — the operator no longer needs to hard-refresh
  // the dashboard after a /settings save to see Flask's new SHA.
  const flaskConfig =
    _flaskConfigSha || cfg.flaskConfigSha || readCell("flask", "config");
  // Pi cells: read from the in-memory `_latestSnapshot` if present
  // (live WS message OR persisted snapshot from /api/sign-status on
  // page load). The snapshot IS the truth — if it has a non-empty
  // `short_sha`, that's the value the Pi was running when it last
  // reported (live or offline-because-of-restart). We do NOT fall
  // back to reading the cell's textContent: that path would surface
  // a previous session's SHA as if it were a fresh signal, and the
  // column drift rule would flip red on stale data. With the fix
  // above (`applyFieldsRender` no longer short-circuits on
  // `state === "offline"`), the Pi cell text and the Pi snapshot
  // value stay in lockstep — when the snapshot has a SHA, the cell
  // shows it; when the snapshot is empty, the cell stays at its
  // server-rendered "—".
  const piHasLiveSnapshot = Boolean(snapshot && snapshot.short_sha);
  const piHasLiveAppliedConfig = Boolean(snapshot && snapshot.applied_config_sha);
  const piCode = (snapshot && snapshot.short_sha) || "";
  const piConfig = (snapshot && snapshot.applied_config_sha) || "";
  const browserCode = readCell("browser", "code");
  // Browser/Config is read from the module cache (`_browserConfigSha`)
  // rather than the cell DOM — applyBrowserConfigReceipt writes the
  // cell ASYNCHRONOUSLY (via PyScript proxy await), but the comparison
  // below runs synchronously. Reading the cache keeps the comparison
  // deterministic regardless of which tick of applyBrowserConfigReceipt
  // has completed.
  const browserConfig = _browserConfigSha || "";

  const cellsByColumn = {
    code: { flask: flaskCode, pi: piCode, browser: browserCode },
    config: { flask: flaskConfig, pi: piConfig, browser: browserConfig },
  };
  // Per-column "which rows are backed by a snapshot signal" map. Pi is
  // live when the in-memory snapshot carries short_sha /
  // applied_config_sha — this is true for both fresh WS messages and
  // for persisted snapshots restored from `sign_status_log` on Flask
  // startup. Flask and Browser are always live (page-rendered +
  // receipt-driven). The drift rule only compares LIVE signals — a
  // Pi cell with no snapshot stays neutral (cell text is "—").
  const liveByColumn = {
    code: { flask: true, pi: piHasLiveSnapshot, browser: true },
    config: { flask: true, pi: piHasLiveAppliedConfig, browser: true },
  };

  // Per-column red-highlight: if any two LIVE cells disagree, every
  // LIVE cell in that column flips red. Empty cells AND cells whose
  // backing source is offline stay neutral — "we don't know yet" is
  // not drift.
  for (const col of VERSION_DRIFT_COLUMNS) {
    const vals = cellsByColumn[col];
    const live = liveByColumn[col];
    const liveVals = Object.entries(vals)
      .filter(([row, v]) => live[row] && v && v.length > 0)
      .map(([, v]) => v);
    const liveAllAgree =
      liveVals.length === 0 ||
      liveVals.every((v) => v === liveVals[0]);
    for (const row of VERSION_DRIFT_ROWS) {
      const el = table.querySelector(
        `[data-version-drift-cell="${row}-${col}"]`
      );
      if (!el) continue;
      const v = vals[row];
      const isLive = live[row];
      const isEmpty = !v || v.length === 0;
      el.classList.remove("text-red-700", "bg-red-100", "text-slate-700");
      if (isLive && !isEmpty && !liveAllAgree) {
        el.classList.add("text-red-700", "bg-red-100");
      } else {
        // Neutral slate when LIVE+agree, LIVE+empty (shouldn't happen),
        // or NOT-LIVE (Pi offline). The cell's textContent is unchanged
        // when not-live — the operator still sees the last-known SHA.
        el.classList.add("text-slate-700");
      }
    }
  }
}

// Module-level caches of the most-recent config_sha the browser has
// seen on the wire, captured from the in-browser MessageManager's
// receipt of Flask's published config envelope. Written by
// `applyBrowserConfigReceipt()` and read by `applyVersionDriftRender`
// so the per-column disagreement logic runs against the same value
// the cells display — without an async-read race against the cell
// write. Initialized to "" so cold start reads as "we don't know yet"
// (treated as empty by the column-agreement rule).
//
// Two caches, one source. The receipt is the SHA Flask just published
// — so by definition the SHA the BROWSER received IS the SHA Flask
// published. We mirror that one value into both the Browser/Config
// cell AND the Flask/Config cell (the latter was previously page-load
// only; now it updates from the receipt too, avoiding a hard-refresh
// after every /settings save). Pi/Config remains driven by the status
// flow at its own 5s cadence.
//
// Source of truth is the MQTT receipt path ONLY. The browser's
// MessageManager receives the same config envelope Flask publishes;
// `_last_applied_config_sha` is captured at receipt time and exposed
// via `App.getLastConfigReceipt()`. If the receipt path is broken,
// both cells stay "—" — that's the correct diagnostic, not something
// to paper over with a /api/config fallback that would hide broker
// fan-out drops. (Operator call: trust the WS path; only refresh on
// actual broker-side breakage, which the red cell will surface.)
let _browserConfigSha = "";
let _flaskConfigSha = "";

// Pull the most-recent browser-applied config_sha from the in-browser
// MessageManager and write it into the Browser/Config cell, the
// Flask/Config cell, and both module caches. Called on every on_change
// fan-out (config envelope arrival) AND on every 5s renderAll tick —
// the tick path is the safety net for cases where the change hook
// missed (PyScript race during cold start). Returns a Promise — the
// callers are fire-and-forget (the cell DOM updates run in a microtask).
async function applyBrowserConfigReceipt() {
  const browserCell = document.querySelector(
    '[data-version-drift-cell="browser-config"]'
  );
  const flaskCell = document.querySelector(
    '[data-version-drift-cell="flask-config"]'
  );
  if (!browserCell && !flaskCell) return;
  // Write the cached value synchronously first so the next synchronous
  // `applyVersionDriftRender` (if scheduled) sees the latest receipt —
  // keeps the cell DOM and the comparison logic in lockstep.
  if (_browserConfigSha.length > 0) {
    if (browserCell) browserCell.textContent = _browserConfigSha;
    if (flaskCell) flaskCell.textContent = _browserConfigSha;
  }
  if (typeof window.App === "undefined" || !window.App.getLastConfigReceipt) {
    // PyScript shim not installed yet — the Browser/Config cell has
    // nothing to write (no cache, no receipt path). The Flask/Config
    // cell keeps its server-rendered value (from /api/config at page
    // load) — do NOT overwrite it with "—". The 5s tick retries
    // until PyScript lands.
    return;
  }
  try {
    const receipt = await window.App.getLastConfigReceipt();
    const sha = (receipt && receipt.sha) || "";
    if (sha && sha.length > 0) {
      _browserConfigSha = sha;
      _flaskConfigSha = sha;
      if (browserCell) browserCell.textContent = sha;
      if (flaskCell) flaskCell.textContent = sha;
    }
    // IMPORTANT: do NOT write "—" when the receipt is empty. The
    // cells already carry their server-rendered / cache values:
    //   - Flask/Config cell: server-rendered `{{ flask_config_sha
    //     or "—" }}` (concrete SHA from /api/config on every page
    //     load) OR the previous receipt-driven value. Overwriting
    //     with "—" on receipt-empty would erase a perfectly good
    //     value just because the receipt path isn't ready yet.
    //   - Browser/Config cell: server-rendered as "—" (no PyScript
    //     access at server-render time). Receipt is the only way
    //     this cell populates; if the receipt is empty, leave the
    //     existing "—" — the 5s tick or on_change will populate it
    //     when the receipt lands.
    // The "wait for receipt" semantics is owned by the 5s tick —
    // `renderAll()` re-invokes this function every 5s, so a missing
    // receipt now is just a deferred write, not a destructive reset.
  } catch (e) {
    console.warn("[sign_status.js] applyBrowserConfigReceipt failed:", e);
  }
}

// Persisted-row badge — visible only when /api/sign-status returned
// source="persisted" (Flask restored from sign_status_log on startup,
// broker hasn't sent anything new since). Shows "Last seen T ago"
// in amber. Flips to display:none once a live WS message lands.
function applyPersistenceBadge(payload) {
  const badge = document.querySelector("[data-version-drift-persisted-badge]");
  if (!badge) return;
  const ageEl = badge.querySelector("[data-version-drift-persisted-age]");
  if (!payload || payload.source !== "persisted" || !payload.received_at) {
    badge.style.display = "none";
    return;
  }
  const t = Date.parse(payload.received_at);
  if (!Number.isFinite(t)) {
    badge.style.display = "none";
    return;
  }
  badge.style.display = "";
  if (ageEl) {
    ageEl.textContent = formatBrowserTimestamp(t) + "";
  }
}

function renderAll(snapshot) {
  const now = Date.now();
  const rendered = combinedState(snapshot, now);
  applyPillRender(rendered);
  applyFieldsRender(snapshot, rendered);
  applyVersionDriftRender(snapshot);
  // Fire-and-forget refresh of the Browser/Config cell from the
  // in-browser receipt. The 5s tick is the safety net for cases
  // where the on_change hook missed the seed-complete or config-
  // envelope events (PyScript race during cold start, broker
  // fan-out drop). `applyBrowserConfigReceipt` is async; this
  // call doesn't block renderAll.
  applyBrowserConfigReceipt();
}

// -----------------------------------------------------------------------------
// Snapshot acceptance — last-write-wins by `updated_at`
// -----------------------------------------------------------------------------

let _latestSnapshot = null;
let _latestReceivedAt = 0; // browser's clock at moment of receipt
let _latestStatusSource = ""; // issue #71 — last /api/sign-status source field

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
    // Issue #71: persist the source field so applyPersistenceBadge
    // can render the "Last seen T ago" amber pill until the first
    // live WS message lands.
    _latestStatusSource = (payload && payload.source) || "";
    applyPersistenceBadge(payload);
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
    // Round 14: AIO `<topic>/get` last-value fetch was REMOVED from
    // mqtt_ws_client.js entirely — both the status and config topic
    // paths now rely on real-time broker delivery (config gets
    // seeded from /api/config on page load; status gets seeded from
    // the persisted /api/sign-status snapshot). The /get workaround
    // was originally added to recover from broker fan-out drops,
    // but the actual root cause was the AIO 1KB payload limit with
    // feed history on — the /get fetch didn't fix that and added
    // confusion by surfacing stale cached data as if it were live.
    onEnvelope: (rawString) => {
      let parsed = null;
      try {
        parsed = JSON.parse(rawString);
      } catch (e) {
        console.warn("[sign_status.js] status WS payload not JSON:", e && e.message ? e.message : e);
        return;
      }
      maybeAcceptSnapshot(parsed);
      // Issue #71: the first live WS message flips source from
      // "persisted" to "live" — hide the "Last seen T ago" amber
      // badge. Re-render once more so the badge element updates.
      if (_latestStatusSource !== "live") {
        _latestStatusSource = "live";
        applyPersistenceBadge({ source: "live" });
      }
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
  // Issue #71: register a change hook so the Browser/Config cell
  // is repopulated every time a new config envelope lands (the
  // `_dispatchChange` fan-out from app.js runs after every
  // MessageManager mutation, including config-envelope apply).
  // `applyBrowserConfigReceipt` is async — we don't await; the
  // 5s tick will catch up if the promise hasn't resolved.
  if (typeof window.App !== "undefined" && typeof window.App.registerOnChange === "function") {
    window.App.registerOnChange(() => {
      applyBrowserConfigReceipt();
    });
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
