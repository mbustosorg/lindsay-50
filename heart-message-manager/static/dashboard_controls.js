// Dashboard lifecycle controls + simulator status binding (issue #48, §5).
//
// Two responsibilities:
//   1. Bind Start / Stop / Restart buttons to `window.Dashboard.start()`,
//      `window.Dashboard.stop()`, `window.Dashboard.restart()`. The
//      controller returns promises; the buttons stay disabled while
//      transitions are in flight so the operator can't enqueue an
//      invalid second click.
//   2. Subscribe to the controller's `on_change` stream and keep the
//      status badge (`#sim-status-badge`), the loading overlay
//      (`#preview-loading`), and the inline error row
//      (`#sim-error-row`) in sync with the active generation's
//      lifecycle state (`starting`/`running`/`stopping`/`stopped`/
//      `error`).
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

  const startBtn = document.getElementById("sim-start-btn");
  const stopBtn = document.getElementById("sim-stop-btn");
  const restartBtn = document.getElementById("sim-restart-btn");
  const badge = document.getElementById("sim-status-badge");
  const errorRow = document.getElementById("sim-error-row");
  const errorMsg = document.getElementById("sim-error-message");
  const errorRetryBtn = document.getElementById("sim-error-retry");
  const loadingEl = document.getElementById("preview-loading");

  // ---- Status → button / badge / error row ----

  function applyState(state, error) {
    if (badge) {
      badge.dataset.state = state || "stopped";
      const labels = {
        starting: "Starting…",
        running: "Running",
        stopping: "Stopping…",
        stopped: "Stopped",
        error: "Error",
      };
      badge.textContent = labels[state] || state || "Stopped";
      const colorClasses = {
        starting: "bg-amber-100 text-amber-700",
        running: "bg-green-100 text-green-700",
        stopping: "bg-amber-100 text-amber-700",
        stopped: "bg-slate-100 text-slate-600",
        error: "bg-red-100 text-red-700",
      };
      // Drop prior state classes, then apply the new one.
      badge.classList.remove(
        "bg-amber-100", "text-amber-700",
        "bg-green-100", "text-green-700",
        "bg-slate-100", "text-slate-600",
        "bg-red-100", "text-red-700",
      );
      const cls = colorClasses[state] || colorClasses.stopped;
      cls.split(" ").forEach((c) => badge.classList.add(c));
    }

    // Buttons are enabled/disabled per state machine:
    //   - starting / running → Stop (and Restart) enabled; Start disabled
    //   - stopping            → nothing enabled (transition in flight)
    //   - stopped             → Start enabled; Stop/Restart disabled
    //   - error               → Start enabled (Retry); Stop/Restart disabled
    const isRunning = state === "starting" || state === "running";
    const isStopping = state === "stopping";
    if (startBtn) startBtn.disabled = isRunning || isStopping;
    if (stopBtn) stopBtn.disabled = !isRunning;
    if (restartBtn) restartBtn.disabled = !isRunning || isStopping;

    // §5.2: when stopped/error, preserve the last frame as a clearly
    // stopped view. The loading overlay text reverts to its initial
    // "press Start" copy on the first stop, then stays hidden on
    // subsequent restarts (a brief blank flash on every restart is
    // bad UX).
    if (loadingEl) {
      if (state === "stopped" || state === "error") {
        loadingEl.style.display = "";
        loadingEl.textContent =
          state === "error"
            ? "Simulator error — see message below. Press Retry to start a new generation."
            : "Simulator stopped — press Start to begin.";
      } else if (state === "starting") {
        loadingEl.style.display = "";
        loadingEl.textContent = "Starting simulator…";
      } else {
        // running: hide the overlay; preview.js clears it on py:done.
        loadingEl.style.display = "none";
      }
    }

    // §1.8: error-state row with actionable retry.
    if (errorRow && errorMsg) {
      if (state === "error") {
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
    window.__PREVIEW_TICK_ENABLED__ = state === "running" || state === "starting";
  }

  // ---- Wire buttons to the controller ----

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
    if (startBtn) {
      startBtn.addEventListener("click", function () {
        // `start()` is async; the button stays disabled until the
        // state transitions back to a non-running state.
        Promise.resolve(dashboard.start()).catch(function (err) {
          console.error("[dashboard-controls] start() rejected:", err);
        });
      });
    }
    if (stopBtn) {
      stopBtn.addEventListener("click", function () {
        Promise.resolve(dashboard.stop()).catch(function (err) {
          console.error("[dashboard-controls] stop() rejected:", err);
        });
      });
    }
    if (restartBtn) {
      restartBtn.addEventListener("click", function () {
        Promise.resolve(dashboard.restart()).catch(function (err) {
          console.error("[dashboard-controls] restart() rejected:", err);
        });
      });
    }
    if (errorRetryBtn) {
      // The retry button is just a Start that the user finds from the
      // error row rather than the toolbar. Same handler, same promise
      // contract.
      errorRetryBtn.addEventListener("click", function () {
        Promise.resolve(dashboard.start()).catch(function (err) {
          console.error("[dashboard-controls] retry start() rejected:", err);
        });
      });
    }
    // Initial state pull + on-change subscription. The controller
    // exposes `status()` returning a state string and `on_change(fn)`
    // for push updates; both are part of the dashboard-controller
    // surface installed by `app_main.py`.
    if (typeof dashboard.status === "function") {
      try {
        const current = dashboard.status();
        applyState(current && current.state, current && current.error);
      } catch (e) {
        console.warn("[dashboard-controls] initial status() failed:", e);
      }
    }
    if (typeof dashboard.on_change === "function") {
      dashboard.on_change(function (snapshot) {
        applyState(snapshot && snapshot.state, snapshot && snapshot.error);
      });
    } else if (typeof dashboard.subscribe === "function") {
      // Older controller builds used `subscribe`. Keep a fallback so
      // this shim survives a controller API rename.
      dashboard.subscribe(function (snapshot) {
        applyState(snapshot && snapshot.state, snapshot && snapshot.error);
      });
    } else {
      console.warn(
        "[dashboard-controls] window.Dashboard exposes neither on_change nor subscribe; " +
          "lifecycle UI will not update after the initial render."
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