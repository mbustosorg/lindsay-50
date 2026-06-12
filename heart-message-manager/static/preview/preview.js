// Main loop + polling for the sign preview.
//
// Three loops run in the browser:
//   1. PyScript's bootstrap, which loads the runtime + preview_main.py and
//      fires `py:ready` (BEFORE the main module runs) and `py:done`
//      (AFTER the main module has finished evaluating) — that's when
//      coordinator.request_message and tick become callable.
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
    // Re-size on viewport changes. We use a ResizeObserver on the card
    // (the bg-white rounded-2xl wrapper) rather than the `window.resize`
    // event because:
    //   - `window.resize` doesn't fire when the URL bar collapses on
    //     mobile (the layout viewport changes but the visual viewport
    //     doesn't), or when the user opens devtools docked to the side
    //     on some browsers.
    //   - The actual available width for the canvas is the card's
    //     content area, not `window.innerWidth`. The card has p-12
    //     (48px on each side) and the dark div inside it has p-4
    //     (16px on each side) — using the card's clientWidth avoids
    //     hardcoding that padding chain and keeps the canvas from
    //     overflowing the card on narrow viewports.
    // rAF-throttled so a continuous drag fires sizeCanvasToViewport at
    // most once per frame instead of dozens of times per second.
    const card = canvas.closest(".bg-white.rounded-2xl");
    if (card && typeof ResizeObserver !== "undefined") {
      let resizeScheduled = false;
      const ro = new ResizeObserver(() => {
        if (resizeScheduled) return;
        resizeScheduled = true;
        requestAnimationFrame(() => {
          resizeScheduled = false;
          sizeCanvasToViewport(canvas);
        });
      });
      ro.observe(card);
    } else {
      // Fallback for browsers without ResizeObserver — listen on window.
      let resizeScheduled = false;
      window.addEventListener("resize", () => {
        if (resizeScheduled) return;
        resizeScheduled = true;
        requestAnimationFrame(() => {
          resizeScheduled = false;
          sizeCanvasToViewport(canvas);
        });
      });
    }

    // Wait for PyScript runtime to be ready.
    //
    // PyScript 2024.9.x dispatches `py:ready` (CustomEvent, bubbles)
    // on each `<py-script>` element in its `connectedCallback` — BEFORE
    // the main module's code runs. At that point `window.pyscript` is
    // not yet populated, so calling `pyscript.globals.get("tick")` will
    // throw "Cannot read properties of undefined (reading 'globals')".
    //
    // The correct post-execution event is `py:done` (CustomEvent, bubbles
    // on each `<py-script>` element) — fired after the main module has
    // finished evaluating, at which point the top-level functions
    // exposed by preview_main.py (request_message, tick, get_frame_rgba)
    // are callable. `py:all-done` is the equivalent plain Event fired
    // once all py-script elements are done.
    //
    // We listen for `py:done` (primary) and `py:ready` (fallback for
    // older runtimes that don't fire `py:done`).
    const onReady = () => {
      hideLoading();
      startRenderLoop(canvas);
      // First poll + ongoing poll, mirroring the testing page pattern:
      //   fetchMessages();
      //   setInterval(fetchMessages, 3000);
      pollLatestMessage();
      setInterval(pollLatestMessage, POLL_MS);
    };
    document.addEventListener("py:done", onReady);
    document.addEventListener("py:all-done", onReady);
    // Backwards-compat for older PyScript releases.
    document.addEventListener("pyodideReady", onReady);
  }

  function sizeCanvasToViewport(canvas) {
    // Cap at 800px on the long edge so the preview fits comfortably on a
    // wide monitor but doesn't blow past 800x800 on a 4K screen. Pixels
    // are scaled with nearest-neighbor (image-rendering: pixelated) so
    // each LED is a discrete block at any output size.
    //
    // The canvas fills the dark div's content area (card width minus
    // the dark div's p-4 padding) up to the 800px cap. We do NOT floor
    // to 64px multiples here — the canvas's CSS `aspect-square` keeps
    // it square regardless of width, and `image-rendering: pixelated`
    // scales the 64x64 frame buffer to whatever pixel size we end up
    // at. Flooring to PANEL_W (64) caused a stair-step effect: the
    // canvas only re-sized in 64px jumps, so the dark border (p-4
    // around the canvas) stuck at a fixed width for most of a resize
    // drag and then snapped suddenly, and the L-R border was visibly
    // wider than the T-B border for the entire 64px range.
    //
    // Layout chain: body → main (flex-1, p-8) → card (block, p-12) →
    //   flex container → dark div (p-4, w-full) → canvas.
    // card.clientWidth = card content + p-12 (48px each side), so
    // subtracting 96 gets the card's content area; subtracting 32 more
    // accounts for the dark div's p-4 (16px each side).
    const max = 800;
    const card = canvas.closest(".bg-white.rounded-2xl");
    let available;
    if (card) {
      available = Math.max(0, card.clientWidth - 96 - 32);
    } else {
      available = Math.max(0, window.innerWidth - 160);
    }
    const size = Math.min(max, available);
    canvas.style.width = size + "px";
    // Clear any previously-set inline height so CSS `h-auto aspect-square`
    // computes the height from the (possibly constrained) width.
    canvas.style.height = "";
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
        //
        // PyScript 2024.9.x removed the `window.pyscript.globals.get("name")`
        // bridge that older releases exposed. The supported pattern is
        // for preview_main.py to install its top-level functions on
        // `js.window`, which is the browser's `window`. The calls below
        // therefore reach `window.tick`, `window.get_frame_rgba`, etc.
        // — installed by preview_main.py when the <py-script> body runs.
        try {
          if (typeof window.tick === "function") window.tick();
          const effectName = typeof window.get_current_effect_name === "function"
            ? window.get_current_effect_name() : "";
          const text = typeof window.get_current_text === "function"
            ? window.get_current_text() : "";
          updateStatus(effectName, text);

          if (typeof window.get_frame_rgba === "function") {
            const bytes = window.get_frame_rgba();
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
          }
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
          // PyScript 2024.9.x removed `window.pyscript`; preview_main.py
          // installs `request_message` on the browser `window`.
          if (typeof window.request_message === "function") {
            window.request_message(body);
          }
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
