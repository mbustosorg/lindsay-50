// Main loop for the sign preview.
//
// Three loops run in the browser:
//   1. PyScript's bootstrap, which loads the runtime + preview_main.py
//      and fires `py:ready` (BEFORE the main module runs) and `py:done`
//      (AFTER the main module has finished evaluating) — that's when
//      coordinator.request_message and tick become callable.
//   2. A requestAnimationFrame loop, capped at 30 FPS, that calls
//      `coordinator.tick()` and blits the frame buffer to the canvas.
//   3. (v2) The base template's `app.js` owns the MQTT-WS client and
//      the in-browser MessageManager. The preview no longer polls
//      /api/live-messages — it registers a callback on the shared
//      MessageManager and drives `coordinator.request_message(body)`
//      from the `on_message` signal.
//
// We DO NOT open a WebSocket from preview.js. The base template's
// app.js owns the WS connection. preview.js only:
//   - Boots the rAF render loop after PyScript is ready.
//   - Registers a per-page on_message callback that hands the new body
//     to the coordinator (with a body-dedupe so we don't re-kick the
//     fade on every tick when nothing has changed).

(function () {
  "use strict";

  // Configuration — matches the device's native cadence.
  const PANEL_W = 64;
  const PANEL_H = 64;
  // Each LED is drawn as a fuzzy circle rather than a hard square. CELL is the
  // backing-store pixels per LED: the 64x64 frame is nearest-neighbor upscaled
  // to CELL-sized solid squares, then clipped to soft circles by a precomputed
  // radial-gradient mask. Higher CELL = crisper dots.
  const CELL = 12;
  const BACK_W = PANEL_W * CELL;   // 768
  const BACK_H = PANEL_H * CELL;
  const FRAME_MS = 1000 / 30;     // 30 FPS cap

  // Module-scope state.
  let lastShownBody = null;
  let lastTick = 0;
  let imgData = null;             // reused ImageData for the rAF blit
  let imgDataNeedsWipe = true;
  let srcCanvas = null;           // 64x64 offscreen holding the raw frame
  let srcCtx = null;
  let dotMask = null;             // precomputed grid of soft circles (alpha mask)
  let callbackRegistered = false;

  // ------------------------------------------------------------------
  // Bootstrap
  // ------------------------------------------------------------------

  function init() {
    const canvas = document.getElementById("sign-canvas");
    if (!canvas) return;
    setupFuzzyRendering(canvas);
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
      registerPreviewCallback();
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
  // Per-page callback: drives coordinator.request_message from the
  // shared in-browser MessageManager's on_message signal.
  // ------------------------------------------------------------------

  function registerPreviewCallback() {
    if (callbackRegistered) return;
    callbackRegistered = true;
    // The base template's app.js exposes `window.App.registerOnMessageCallback`.
    // It wires the in-browser MessageManager's on_message signal to a
    // user-supplied function. The body of the Message is forwarded to
    // the PyScript coordinator; config-shaped payloads (carrying
    // effect_settings / text_settings blocks) are routed to the
    // Python-side `apply_config` so the preview rotation + scroller
    // re-bind live.
    if (window.App && typeof window.App.registerOnMessageCallback === "function") {
      window.App.registerOnMessageCallback((msg) => {
        console.log("[preview] onMessage callback fired, msg=", msg,
          "type=", msg && msg.type,
          "keys=", msg && typeof msg === "object" ? Object.keys(msg) : null,
          "has_effect_settings=", !!(msg && msg.effect_settings));
        // A config envelope is identified by presence of effect_settings
        // (the wire-shape marker). Send it to Python; everything else is
        // a message envelope.
        if (msg && msg.effect_settings) {
          try {
            console.log("[preview] -> calling apply_config");
            if (typeof window.apply_config === "function") {
              window.apply_config(msg);
              console.log("[preview] apply_config returned");
            } else {
              console.warn("[preview] window.apply_config is not a function");
            }
          } catch (e) {
            console.error("apply_config error:", e);
          }
          return;
        }
        const body = msg && msg.body;
        if (body === undefined || body === null || body === "") return;
        if (body === lastShownBody) return;     // dedup
        lastShownBody = body;
        try {
          if (typeof window.request_message === "function") {
            window.request_message(body);
          }
        } catch (e) {
          console.error("request_message error:", e);
        }
      });
    } else {
      // Fallback: app.js hasn't loaded yet (shouldn't happen in v2
      // because base.html loads it before this script). Log and
      // continue — the preview's render loop will still tick.
      console.warn("window.App not available; preview won't receive MQTT envelopes");
    }
    // Seed the preview with the most recent message from the in-browser
    // ring buffer. Without this, the preview shows "Idle" until a fresh
    // MQTT envelope arrives — on a page reload, the buffer is still
    // populated (it was just wiped + re-seeded from /api/messages), so
    // the latest body should be kicked onto the coordinator. Skipped
    // if the buffer is empty (no messages yet) or if a live envelope
    // arrived before the seed completed (lastShownBody will be set and
    // dedup will skip the redundant call).
    seedPreviewFromBuffer();
    // Seed the preview with the current config (effect rotation, scroller
    // color/speed). Idempotent: apply_config replaces the rotation in
    // place and the scroller re-binds, so a second call is safe.
    seedPreviewFromConfig();
  }

  async function seedPreviewFromBuffer() {
    if (!window.App || typeof window.App.getMessages !== "function") return;
    try {
      const msgs = await window.App.getMessages(1, true);
      if (!msgs || msgs.length === 0) return;
      const body = msgs[0].body;
      if (body === undefined || body === null || body === "") return;
      if (body === lastShownBody) return;       // dedup vs. the live callback
      lastShownBody = body;
      if (typeof window.request_message === "function") {
        window.request_message(body);
      }
    } catch (e) {
      console.warn("seedPreviewFromBuffer failed:", e);
    }
  }

  async function seedPreviewFromConfig() {
    if (!window.App || typeof window.App.getConfig !== "function") return;
    try {
      const cfg = await window.App.getConfig();
      if (!cfg) return;
      if (typeof window.apply_config === "function") {
        window.apply_config(cfg);
      }
    } catch (e) {
      console.warn("seedPreviewFromConfig failed:", e);
    }
  }

  // ------------------------------------------------------------------
  // Fuzzy-circle ("LED") rendering setup
  // ------------------------------------------------------------------

  function setupFuzzyRendering(canvas) {
    // The backing store is the high-res circle canvas; CSS scales it to fit
    // the viewport. We render circles ourselves, so turn off the browser's
    // nearest-neighbor upscale (which produced hard squares).
    canvas.width = BACK_W;
    canvas.height = BACK_H;
    canvas.style.imageRendering = "auto";

    // Small offscreen canvas that receives the raw 64x64 frame each tick.
    srcCanvas = document.createElement("canvas");
    srcCanvas.width = PANEL_W;
    srcCanvas.height = PANEL_H;
    srcCtx = srcCanvas.getContext("2d");

    dotMask = buildDotMask();
  }

  function buildDotMask() {
    // One radial-gradient circle per LED cell: opaque core fading to fully
    // transparent at the cell edge. Used once per frame as a destination-in
    // alpha mask, so each solid color square becomes a soft circle with dark
    // gaps between LEDs. Built once — the per-cell gradients are not cheap.
    const mask = document.createElement("canvas");
    mask.width = BACK_W;
    mask.height = BACK_H;
    const mc = mask.getContext("2d");
    const r = CELL * 0.5;          // inscribed in the cell -> gaps at corners
    for (let gy = 0; gy < PANEL_H; gy++) {
      for (let gx = 0; gx < PANEL_W; gx++) {
        const cx = gx * CELL + CELL / 2;
        const cy = gy * CELL + CELL / 2;
        const g = mc.createRadialGradient(cx, cy, 0, cx, cy, r);
        g.addColorStop(0.0, "rgba(255,255,255,1)");
        g.addColorStop(0.55, "rgba(255,255,255,1)");  // solid core
        g.addColorStop(1.0, "rgba(255,255,255,0)");   // soft, fuzzy edge
        mc.fillStyle = g;
        mc.fillRect(gx * CELL, gy * CELL, CELL, CELL);
      }
    }
    return mask;
  }

  function blitFuzzy(ctx, view) {
    // 1) put the raw 64x64 frame on the small offscreen canvas
    if (imgDataNeedsWipe || !imgData) {
      imgData = srcCtx.createImageData(PANEL_W, PANEL_H);
      imgDataNeedsWipe = false;
    }
    imgData.data.set(view);
    srcCtx.putImageData(imgData, 0, 0);

    // 2) nearest-neighbor upscale to solid CELL-sized squares (each LED keeps
    //    its own discrete color — no bleeding into neighbors)
    ctx.globalCompositeOperation = "source-over";
    ctx.clearRect(0, 0, BACK_W, BACK_H);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(srcCanvas, 0, 0, PANEL_W, PANEL_H, 0, 0, BACK_W, BACK_H);

    // 3) clip each square to a soft circle via the precomputed dot mask
    ctx.globalCompositeOperation = "destination-in";
    ctx.drawImage(dotMask, 0, 0);
    ctx.globalCompositeOperation = "source-over";
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
            // object. Render it as fuzzy LED circles (see blitFuzzy).
            const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
            blitFuzzy(ctx, view);
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
  // Go
  // ------------------------------------------------------------------

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
