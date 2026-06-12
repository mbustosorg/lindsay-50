// Main loop + polling for the sign preview.
//
// Three loops run in the browser:
//   1. PyScript's bootstrap, which loads the runtime + preview_main.py and
//      fires the `pyodideReady` event when the coordinator is callable.
//   2. A requestAnimationFrame loop, capped at 30 FPS, that calls
//      `coordinator.tick()` and blits the frame buffer to the canvas.
//   3. A setInterval polling loop that fetches /api/live-messages every
//      3s and hands a new body to the coordinator (with body-dedupe so we
//      don't re-kick the fade on every tick when nothing has changed).
//
// We DO NOT open a WebSocket. v1 is polling only.

(function () {
  "use strict";

  // Configuration — matches the device's native cadence.
  const PANEL_W = 64;
  const PANEL_H = 64;
  const FRAME_MS = 1000 / 30;     // 30 FPS cap
  const POLL_MS = 3000;            // match templates/testing.html
  const POLL_URL = "/api/live-messages?limit=1&suppress=true";

  // Module-scope state. `lastShownBody` is the dedup anchor: the polling
  // loop compares each fetched body against this and only calls
  // coordinator.request_message() when they differ.
  let lastShownBody = null;
  let lastTick = 0;
  let imgData = null;             // reused ImageData for the rAF blit
  let imgDataNeedsWipe = true;

  // ------------------------------------------------------------------
  // Bootstrap
  // ------------------------------------------------------------------

  function init() {
    const canvas = document.getElementById("sign-canvas");
    if (!canvas) return;
    sizeCanvasToViewport(canvas);
    // Re-size on viewport changes (window resize, devtools open/close,
    // device rotation on touch). rAF-throttled so a continuous drag fires
    // sizeCanvasToViewport at most once per frame instead of dozens of
    // times per second.
    let resizeScheduled = false;
    window.addEventListener("resize", () => {
      if (resizeScheduled) return;
      resizeScheduled = true;
      requestAnimationFrame(() => {
        resizeScheduled = false;
        sizeCanvasToViewport(canvas);
      });
    });

    // Wait for PyScript runtime to be ready. PyScript 2024.10+ exposes a
    // `pyodideReady` event when its main module has finished evaluating
    // (which is when coordinator.request_message and tick are callable).
    document.addEventListener("pyodideReady", () => {
      hideLoading();
      startRenderLoop(canvas);
      // First poll + ongoing poll, mirroring the testing page pattern:
      //   fetchMessages();
      //   setInterval(fetchMessages, 3000);
      pollLatestMessage();
      setInterval(pollLatestMessage, POLL_MS);
    });
  }

  function sizeCanvasToViewport(canvas) {
    // Cap at 800px on the long edge so the preview fits comfortably on a
    // wide monitor but doesn't blow past 800x800 on a 4K screen. Pixels
    // are scaled with nearest-neighbor (image-rendering: pixelated) so
    // each LED is a discrete block.
    const max = 800;
    const available = Math.min(max, window.innerWidth - 64);
    const n = Math.max(1, Math.floor(available / PANEL_W));
    const size = Math.min(max, n * PANEL_W);
    canvas.style.width = size + "px";
    canvas.style.height = size + "px";
  }

  function hideLoading() {
    const loading = document.getElementById("preview-loading");
    if (loading) loading.style.display = "none";
  }

  // ------------------------------------------------------------------
  // Render loop (rAF, 30 FPS cap)
  // ------------------------------------------------------------------

  function startRenderLoop(canvas) {
    const ctx = canvas.getContext("2d");

    function frame(now) {
      if (now - lastTick >= FRAME_MS) {
        // Call Python: advance the coordinator, then pull the frame buffer.
        // pyodide.globals.get / .getattr works on PyScript 2024.10+.
        try {
          window.pyscript.globals.get("tick")();
          const effectName = window.pyscript.globals.get("get_current_effect_name")();
          const text = window.pyscript.globals.get("get_current_text")();
          updateStatus(effectName, text);

          const bytes = window.pyscript.globals.get("get_frame_rgba")();
          // `bytes` is a Pyodide-converted Uint8Array view; in plain
          // CPython (impossible here, but defensive) it would be a bytes
          // object. Build an ImageData the first time, then reuse it.
          if (imgDataNeedsWipe || !imgData) {
            imgData = ctx.createImageData(PANEL_W, PANEL_H);
            imgDataNeedsWipe = false;
          }
          const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
          imgData.data.set(view);
          ctx.putImageData(imgData, 0, 0);
        } catch (e) {
          console.error("Frame error:", e);
        }
        lastTick = now;
      }
      requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function updateStatus(effectName, text) {
    const e = document.getElementById("preview-effect");
    if (e && effectName !== undefined) e.textContent = effectName || "—";
    const m = document.getElementById("preview-message");
    if (m) m.textContent = (text && text.length) ? text : "Idle";
  }

  // ------------------------------------------------------------------
  // Polling (3s, mirrors templates/testing.html)
  // ------------------------------------------------------------------

  function pollLatestMessage() {
    fetch(POLL_URL, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) {
        if (!Array.isArray(data) || data.length === 0) {
          // Empty list — keep coordinator state, don't re-kick the fade.
          return;
        }
        const body = data[0].body;
        if (body === undefined || body === null || body === "") return;
        if (body === lastShownBody) return;     // dedup
        lastShownBody = body;
        try {
          window.pyscript.globals.get("request_message")(body);
        } catch (e) {
          console.error("request_message error:", e);
        }
      })
      .catch(function (e) {
        console.warn("Live-messages poll failed:", e);
      });
  }

  // ------------------------------------------------------------------
  // Go
  // ------------------------------------------------------------------

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
