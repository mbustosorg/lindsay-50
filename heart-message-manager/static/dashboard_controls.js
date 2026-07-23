// Dashboard lifecycle controls + simulator status binding (issue #48, §5).
//
// Responsibilities:
//   1. Wire the single Start/Stop toggle button (`#sim-toggle-btn`,
//      rendered inline at the top-right of the preview card, next to
//      the "Now displaying" line) to `window.Dashboard.start()` /
//      `.stop()`.
//   2. Subscribe to the controller's state stream and keep the
//      loading overlay (`#preview-loading`), the inline error row
//      (`#sim-error-row`), and the toggle button itself in sync with
//      the active generation's lifecycle state
//      (`starting`/`running`/`stopping`/`stopped`/`error`).
//
// The button label flips `Start` ⇄ `Stop` based on the runtime state.
// It is disabled during `stopping` so the operator can't enqueue a
// second transition. The simulator status badge was removed
// alongside the Simulator runtime card (post-2026-07-22 follow-up)
// — the button label + error row are the only lifecycle UI now.
//
// §5.5: the page-local rAF render loop in `preview.js` reads
// `window.__PREVIEW_TICK_ENABLED__` before invoking `window.tick()`.
// We toggle that flag from this shim so the canvas stops animating
// during Stop / Stopped / Error states (the last frame is preserved
// as a clearly-stopped view, §5.2).
//
// The script is a no-op on pages that lack the `[data-dashboard-controls]`
// marker, so the same file is safe to load on /messages, /settings,
// /testing if a future template wants the lifecycle UI there too.

(function () {
  "use strict";

  const root = document.querySelector("[data-dashboard-controls]");
  if (!root) {
    // No-op on pages that don't host the dashboard.
    return;
  }

  const toggleBtns = Array.from(
    document.querySelectorAll("#sim-toggle-btn"),
  );
  const errorRow = document.getElementById("sim-error-row");
  const errorMsg = document.getElementById("sim-error-message");
  const errorRetryBtn = document.getElementById("sim-error-retry");
  const loadingEl = document.getElementById("preview-loading");

  // Status → label / disabled of the TOGGLE BUTTON (Start vs Stop).
  //
  // A single toggle button encodes "the next action" — given the
  // current state, what should the button's click do? Stopped /
  // Error → click triggers a Start. Running / Starting → click
  // triggers a Stop. Stopping → disabled. The simulator status badge
  // was removed alongside the runtime card, so the button label is
  // the only "what state is the simulator in?" indicator.
  const TOGGLE_LABEL = {
    stopped: "Start",
    starting: "Stop",
    running: "Stop",
    stopping: "Stop",
    error: "Start",
  };

  // ---- Status → button / error row ----

  function applyState(state, error) {
    const normalized = state || "stopped";

    // Toggle button label + disabled state — encoded directly from
    // the controller's lifecycle state.
    toggleBtns.forEach((btn) => {
      btn.textContent = TOGGLE_LABEL[normalized] || "Start";
      // Only disabled during `stopping` — every other state accepts
      // a click (the click does the right thing for each).
      btn.disabled = normalized === "stopping";
    });

    // §5.2: when stopped/error, preserve the last frame as a clearly
    // stopped view. The loading overlay text reverts to its initial
    // "press Start" copy on the first stop, then stays hidden on
    // subsequent restarts (a brief blank flash on every restart is
    // bad UX).
    if (loadingEl) {
      if (normalized === "stopped" || normalized === "error") {
        loadingEl.style.display = "";
        loadingEl.textContent =
          normalized === "error"
            ? "Simulator error — see message below. Press Start to retry."
            : "Simulator stopped — press Start to begin.";
      } else if (normalized === "starting") {
        loadingEl.style.display = "";
        loadingEl.textContent = "Starting simulator…";
      } else {
        // running / stopping: hide the overlay; preview.js clears it
        // on py:done.
        loadingEl.style.display = "none";
      }
    }

    // §1.8: error-state row with actionable retry.
    if (errorRow && errorMsg) {
      if (normalized === "error") {
        errorMsg.textContent =
          (error && (error.message || String(error))) ||
          "Unknown error (see browser console).";
        errorRow.classList.remove("hidden");
      } else {
        errorRow.classList.add("hidden");
        errorMsg.textContent = "";
      }
    }

    // §5.5: gate the rAF render loop. `preview.js` reads this flag
    // before calling `window.tick()` so the canvas freezes cleanly
    // on Stop / Stopped / Error.
    window.__PREVIEW_TICK_ENABLED__ =
      normalized === "running" || normalized === "starting";
  }

  // ---- Wire buttons to the controller ----

  function handleClick() {
    // Pick the right action from the current state. `state()` returns
    // the raw lifecycle string; the click handler reads it once and
    // dispatches. We avoid baking the decision into per-button
    // listeners — both toolbar + canvas buttons share this handler.
    const dashboard = window.Dashboard;
    if (!dashboard || typeof dashboard.state !== "function") return;
    let current = "stopped";
    try {
      current = dashboard.state();
    } catch (e) {
      console.warn("[dashboard-controls] state() failed:", e);
      return;
    }
    const action =
      current === "running" || current === "starting" ? "stop" : "start";
    const fn = dashboard[action];
    if (typeof fn !== "function") {
      console.warn("[dashboard-controls] Dashboard." + action + " missing");
      return;
    }
    Promise.resolve(fn.call(dashboard)).catch(function (err) {
      console.error("[dashboard-controls] " + action + "() rejected:", err);
    });
  }

  function bind() {
    const dashboard = window.Dashboard;
    if (!dashboard) {
      // PyScript hasn't installed `window.Dashboard` yet — try again
      // after a short delay. The bootstrap in `app_main.py` runs
      // before `py:done` fires, so this race resolves within a few
      // hundred ms in practice.
      window.setTimeout(bind, 50);
      return;
    }
    toggleBtns.forEach((btn) => {
      btn.addEventListener("click", handleClick);
    });
    if (errorRetryBtn) {
      // The Retry button is a Start that the operator finds from the
      // error row rather than the toolbar. Same dispatch path.
      errorRetryBtn.addEventListener("click", handleClick);
    }
    // Initial state pull + on-change subscription. The controller
    // exposes `state()` returning a state string — older builds
    // returned `{state, error}` via a `status()` / `subscribe`
    // pair, so we accept either shape for forward/backward compat.
    function snapshot() {
      try {
        const s = dashboard.state();
        if (s && typeof s === "object") return s;
        return { state: s, error: null };
      } catch (e) {
        console.warn("[dashboard-controls] state() failed:", e);
        return { state: "stopped", error: null };
      }
    }
    if (typeof dashboard.status === "function") {
      try {
        const current = dashboard.status();
        applyState(current && current.state, current && current.error);
      } catch (e) {
        console.warn("[dashboard-controls] initial status() failed:", e);
      }
    } else {
      applyState((snapshot() || {}).state);
    }
    if (typeof dashboard.on_change === "function") {
      dashboard.on_change(function (snap) {
        applyState(snap && snap.state, snap && snap.error);
      });
    } else if (typeof dashboard.subscribe === "function") {
      // Older controller builds used `subscribe`. Keep a fallback so
      // this shim survives a controller API rename.
      dashboard.subscribe(function (snap) {
        applyState(snap && snap.state, snap && snap.error);
      });
    } else {
      // No push channel available — do one initial pull and let the
      // first click re-pull. Not ideal, but doesn't crash the page.
      console.warn(
        "[dashboard-controls] window.Dashboard exposes neither on_change " +
          "nor subscribe; badge will lag by one click.",
      );
    }
    // Initial gate: default to disabled until the controller reports
    // a real state. Without this, the canvas rAF loop would happily
    // call `window.tick()` while PyScript is still loading and crash
    // on `_coord() == None`.
    window.__PREVIEW_TICK_ENABLED__ = false;
    console.log("[dashboard-controls] bound to window.Dashboard");
  }

  // The controller lives behind a PyScript top-level await; we don't
  // try to bind until DOMContentLoaded fires so the buttons exist in
  // the DOM. After that, the `window.Dashboard` poll handles the
  // PyScript bootstrap race.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
