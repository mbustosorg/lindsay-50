// Main loop for the sign preview.
//
// Three loops run in the browser:
//   1. PyScript's bootstrap, which loads the runtime + preview_main.py
//      and fires `py:ready` (BEFORE the main module runs) and `py:done`
//      (AFTER the main module has finished evaluating) — that's when
//      coordinator.tick becomes callable.
//   2. A requestAnimationFrame loop, capped at 30 FPS, that calls
//      `coordinator.tick()` and blits the frame buffer to the canvas.
//   3. (v2) The base template's `app.js` owns the MQTT-WS client and
//      the in-browser MessageManager. The preview no longer polls
//      /api/live-messages, and it no longer pushes the next body or
//      the current config to the coordinator — the per-page
//      MessageManager constructed by `preview_main.py` wires the
//      manager's universal `on_change` directly to
//      `coord.apply_settings(...)`. The coordinator pulls the next
//      display message from the manager on a 250 ms throttle (see
//      `EffectsCoordinator.get_display_message`).
//
// We DO NOT open a WebSocket from preview.js. The base template's
// app.js owns the WS connection. preview.js only:
//   - Boots the rAF render loop after PyScript is ready.

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
  let lastTick = 0;
  let imgData = null;             // reused ImageData for the rAF blit
  let imgDataNeedsWipe = true;
  let srcCanvas = null;           // 64x64 offscreen holding the raw frame
  let srcCtx = null;
  let dotMask = null;             // precomputed grid of soft circles (alpha mask)

  // ------------------------------------------------------------------
  // Bootstrap
  // ------------------------------------------------------------------

  function init() {
    console.log("[preview-js] init() entered; canvas=" + !!document.getElementById("sign-canvas"));
    const canvas = document.getElementById("sign-canvas");
    if (!canvas) { console.error("[preview-js] #sign-canvas not found; aborting"); return; }
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
    // exposed by preview_main.py (tick, get_frame_rgba,
    // get_current_text, get_current_effect_name) are callable.
    // `py:all-done` is the equivalent plain Event fired once all
    // py-script elements are done.
    //
    // We listen for `py:done` (primary) and `py:ready` (fallback for
    // older runtimes that don't fire `py:done`).
    const onReady = () => {
      hideLoading();
      console.log("[preview-js] onReady fired; starting render loop");
      startRenderLoop(canvas);
    };
    document.addEventListener("py:done", () => console.log("[preview-js] received py:done event"));
    document.addEventListener("py:done", onReady);
    document.addEventListener("py:all-done", () => console.log("[preview-js] received py:all-done event"));
    document.addEventListener("py:all-done", onReady);
    // Backwards-compat for older PyScript releases.
    document.addEventListener("pyodideReady", () => console.log("[preview-js] received pyodideReady event"));
    document.addEventListener("pyodideReady", onReady);
    // Surface PyScript module-eval failures (the reason py:done
    // never fires). PyScript dispatches a CustomEvent with the
    // failing element's details if a module throws during
    // evaluation; without this listener, the preview sits on
    // "Loading preview…" forever with no console signal.
    document.addEventListener("py:error", (e) => {
      console.error("[preview-js] py:error event — PyScript module evaluation failed:", e);
    });
    window.addEventListener("error", (e) => {
      console.error("[preview-js] window.error:", e.message, e.filename, e.lineno);
    });
    window.addEventListener("unhandledrejection", (e) => {
      console.error("[preview-js] unhandled promise rejection:", e.reason);
    });
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
  // No per-page re-render shim. The per-page MessageManager
  // constructed by `preview_main.py` wires its universal
  // `on_change` directly to `coord.apply_settings(...)`, which
  // is the single source of truth for "the manager's state just
  // changed — re-apply the config and let the coordinator's
  // throttled pull pick the next body". The JS side no longer
  // pushes via `request_message` or `apply_config`; the preview
  // no longer registers an `onChange` listener here.
  // ------------------------------------------------------------------

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
    console.log("[preview-js] startRenderLoop entered");
    const ctx = canvas.getContext("2d");
    const mediaImg = document.getElementById("browser-media-image");
    const mediaVideo = document.getElementById("browser-media-video");
    const messageLink = document.getElementById("preview-message-link");
    if (messageLink) {
      // Wire the modal click directly via addEventListener. The
      // previous inline `onclick="return showPreviewMessageModal(event)"`
      // approach was fragile: it relied on `this` resolving to the
      // anchor after the click bubbled from the inner <span>, and on
      // the inline handler not being clobbered by anything that
      // touches attributes. A direct listener on the anchor is the
      // robust path — `this` is always the anchor, the listener
      // can't be shadowed by template edits, and we can `preventDefault`
      // to suppress the href="#" navigation.
      messageLink.addEventListener("click", function (ev) {
        ev.preventDefault();
        const raw = messageLink.dataset.msg;
        if (!raw) {
          console.warn("[preview-js] message link clicked but no data-msg; idle state?");
          return;
        }
        try {
          const item = JSON.parse(decodeURIComponent(escape(atob(raw))));
          document.getElementById("json-modal-title").textContent = "Message " + item.id;
          document.getElementById("json-modal-body").textContent = JSON.stringify(item, null, 2);
          const modal = document.getElementById("json-modal");
          if (!modal) {
            console.error("[preview-js] #json-modal element not found");
            return;
          }
          modal.classList.remove("hidden");
          modal.classList.add("flex");
          console.log("[preview-js] modal opened for message id=%s", item.id);
        } catch (e) {
          console.error("[preview-js] modal decode failed:", e, "raw=", raw.slice(0, 80));
        }
      });
      console.log("[preview-js] message-link click handler attached");
    } else {
      console.error("[preview-js] #preview-message-link element not found");
    }
    let lastMediaKey = "";
    let lastEffectNameForLog = "";
    // Tracks the id of the message currently bound to the status
    // link. The link's `data-msg` is rewritten only when this
    // changes — the rAF loop at 30 FPS would otherwise base64-
    // encode the full Message dict every frame even when the
    // coordinator is sitting on the same message for 15+ seconds.
    let lastLinkMessageId = "";
    let lastEmptyLogAt = 0;
    // Coordinator-state heartbeat: lets the developer console
    // show the live state-machine values (mode, scroller brightness,
    // media opacity, phase elapsed) at 1 Hz so you can correlate
    // what's on screen with what's happening in the fade ramp.
    // Set `window.__PREVIEW_DEBUG__ = false` to silence these logs.
    let lastDiagnosticsAt = 0;
    let lastDiagnosticsKey = "";

    // Diagnostic flag — set to `false` in devtools or by overriding
    // `window.__PREVIEW_DEBUG__ = false` to silence the overlay
    // trace. Logs only fire on STATE CHANGES (effect name swap,
    // media key swap, overlay hide) and throttled "no media"
    // notices once per second, so the rAF loop at 30 FPS doesn't
    // spam the console.
    const PREVIEW_DEBUG = (typeof window.__PREVIEW_DEBUG__ === "undefined")
      ? true : !!window.__PREVIEW_DEBUG__;

    function applyMedia(media) {
      // Browser-side media overlay (issue #38). `preview_main.py`
      // exposes `BrowserMediaOverlay.current_media_url` /
      // `current_media_kind` / `current_opacity` via
      // `window.get_current_media()`. The overlay DOM elements sit
      // above the canvas; the LED-fuzzy render underneath stays
      // visible when the overlay is transparent. When `url` is empty
      // (no picked media, or the cycler is exhausted), both
      // elements are hidden and the canvas shows through.
      if (!mediaImg || !mediaVideo) return;
      const url = (media && media.url) || "";
      const kind = (media && media.kind) || "";
      const opacity = (media && typeof media.opacity === "number") ? media.opacity : 0;
      const key = (media && media.key) || "";

      if (!url || !kind) {
        // No active media — hide both, leave the canvas alone.
        if (!mediaImg.hidden) mediaImg.hidden = true;
        if (!mediaVideo.hidden) mediaVideo.hidden = true;
        if (PREVIEW_DEBUG) {
          // Throttle the "hiding overlay" log to once per second.
          // The previous implementation gated this on key-change
          // and reset `lastMediaKey = ""` inside, which meant
          // the log never fired when `key` was persistently ""
          // (the symptom we're trying to diagnose — overlay
          // returns empty url on every frame). The time-based
          // throttle gives us a steady signal we can correlate
          // with the "python returned empty" log.
          const now = Date.now();
          if (now - lastEmptyLogAt > 1000) {
            console.log(
              "[preview-media] hiding overlay: url=%s kind=%s key=%s (no picked media, or BrowserMediaOverlay returned empty url)",
              url, kind, key,
            );
            lastEmptyLogAt = now;
          }
        }
        lastMediaKey = "";
        return;
      }

      // Swap src only when the underlying item changed — reassigning
      // the same URL forces a re-decode in some browsers and creates
      // a flash. The S3 key (`media.key`) is the stable identifier.
      if (key !== lastMediaKey) {
        if (PREVIEW_DEBUG) {
          console.log(
            `[preview-media] swapping ${kind} src: key=${key} url=${url} opacity=${Number(opacity || 0).toFixed(2)}`,
          );
        }
        if (kind === "image") {
          if (!mediaImg.hidden) mediaImg.hidden = true;
          mediaImg.src = url;
          mediaVideo.removeAttribute("src");
          mediaVideo.load();
          mediaImg.hidden = false;
          // Surface load failures in the console — the most common
          // cause is a 4xx from the Flask `/api/media/<key>` proxy
          // (S3 key mismatch, missing auth, CORS, etc.).
          mediaImg.addEventListener("error", function onErr() {
            mediaImg.removeEventListener("error", onErr);
            console.error(
              "[preview-media] <img> failed to load url=%s key=%s — check the Flask /api/media/<key> response and the S3 object exists",
              url, key,
            );
          }, { once: true });
        } else if (kind === "video") {
          if (!mediaVideo.hidden) mediaVideo.hidden = true;
          mediaImg.removeAttribute("src");
          mediaVideo.src = url;
          mediaVideo.load();
          // `muted`+`playsinline`+`autoplay` already on the element;
          // calling play() resumes after the load promise resolves.
          const playPromise = mediaVideo.play();
          if (playPromise && typeof playPromise.then === "function") {
            playPromise.catch((err) => {
              console.warn(
                "[preview-media] <video> play() rejected url=%s key=%s (often: autoplay without user gesture): %s",
                url, key, err && err.message ? err.message : err,
              );
            });
          }
          mediaVideo.addEventListener("error", function onErr() {
            mediaVideo.removeEventListener("error", onErr);
            console.error(
              "[preview-media] <video> failed to load url=%s key=%s — check codec, Flask proxy response, and S3 object",
              url, key,
            );
          }, { once: true });
          mediaVideo.hidden = false;
        }
        lastMediaKey = key;
      }

      // Apply opacity (tracks `set_brightness` from the coordinator).
      mediaImg.style.opacity = String(opacity);
      mediaVideo.style.opacity = String(opacity);
    }

    function updateMessageLink() {
      // Bind the preview status text to a clickable link that
      // opens the Testing page's #json-modal showing the full
      // Message dict (id, sender, body, received_at, media).
      //
      // Cheap path: the rAF loop is 30 FPS but a typical
      // `hold_seconds` is 15s, so the picked message is
      // stable for ~450 frames in a row. We only re-encode
      // the base64 JSON when the id changes — and we cache
      // the id in `lastLinkMessageId` so the hot path is a
      // single integer compare.
      if (!messageLink) return;
      if (typeof window.get_current_message !== "function") return;
      let msg = null;
      try {
        msg = window.get_current_message();
      } catch (e) {
        if (PREVIEW_DEBUG) console.warn("[preview-modal] get_current_message failed:", e);
        return;
      }
      // `get_current_message` is wrapped in `to_js(dict_converter=Object.fromEntries)`
      // on the Python side (preview_main.py), so what arrives here is a plain
      // JS `Object` with property accessors — `msg.id`, `JSON.stringify(msg)`,
      // etc. all work. Pyodide's default Python-dict-to-JS conversion produces
      // a Map, which is why we wrap on the Python side instead.
      const id = (msg && msg.id) || "";
      if (id === lastLinkMessageId) {
        // Hot path: same id as last frame, do nothing. This is the
        // 99% case during a 15s hold.
        return;
      }
      if (PREVIEW_DEBUG) {
        // Log the picked message so we can see in the console what
        // the wire shape looks like — specifically the `media` list,
        // which is the field the image render path depends on. If
        // `media` is empty, the image can never load (the coordinator
        // won't construct a `BrowserMediaOverlay`); if `media` has
        // entries but the image still doesn't render, the issue is
        // downstream in `applyMedia` / the Flask `/api/media/<key>`
        // proxy.
        const media = (msg && Array.isArray(msg.media)) ? msg.media : [];
        const mediaUrls = media.map((m) => m && m.url).filter(Boolean);
        console.log(
          "[preview-modal] picked message: id=%s body=%s media_count=%d media_urls=%s",
          id || "(none)",
          ((msg && msg.body) || "").slice(0, 40),
          media.length,
          JSON.stringify(mediaUrls),
        );
      }
      lastLinkMessageId = id;
      if (!msg || !id) {
        // Idle / no picked entry — drop the data-msg so the
        // click handler is a no-op and revert the link to plain-
        // text styling so "Now displaying: Idle" doesn't read as
        // a link to nowhere.
        delete messageLink.dataset.msg;
        messageLink.className = "text-slate-500";
        return;
      }
      // Match the encode/decode in preview.html:
      //   encode: btoa(unescape(encodeURIComponent(JSON.stringify(item))))
      //   decode: JSON.parse(decodeURIComponent(escape(atob(raw))))
      // — the unescape/escape dance round-trips UTF-8 safely.
      try {
        const json = JSON.stringify(msg);
        const b64 = btoa(unescape(encodeURIComponent(json)));
        messageLink.dataset.msg = b64;
        // Switch the link to the indigo/underline treatment so
        // the user can see it's clickable. Set as the full
        // className so the previous `text-slate-500` is dropped.
        messageLink.className = "text-indigo-600 hover:text-indigo-800 hover:underline cursor-pointer";
      } catch (e) {
        if (PREVIEW_DEBUG) console.warn("[preview-modal] encode failed:", e);
        delete messageLink.dataset.msg;
        messageLink.className = "text-slate-500";
      }
    }

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
          updateMessageLink();
          if (PREVIEW_DEBUG && effectName !== lastEffectNameForLog) {
            console.log("[preview-effect] now=%s (was=%s)", effectName || "—", lastEffectNameForLog || "—");
            lastEffectNameForLog = effectName;
          }

          // Pull the active media attachment (issue #38). When the
          // picked message has MMS attachments, the coordinator's
          // `BrowserMediaOverlay` exposes them here; `preview.js`
          // swaps the DOM `<img>` / `<video>` element's `src` to
          // match. No-ops cleanly for SMS-only messages.
          if (typeof window.get_current_media === "function") {
            const media = window.get_current_media();
            if (PREVIEW_DEBUG) {
              // Two distinct log paths so the diagnostic actually
              // fires in the case we care about:
              //
              //   1. Non-empty payload (key/url present): log on
              //      every key change — the rAF loop at 30 FPS
              //      would otherwise spam the console during a
              //      15s hold. This is the "happy path" diagnostic
              //      that confirms the overlay is producing a real
              //      URL.
              //
              //   2. Empty payload (key/url are ""): the previous
              //      implementation gated this on key-change too,
              //      which meant the log NEVER fired when Python
              //      was persistently returning the empty stub
              //      (the symptom we're trying to diagnose — the
              //      picked message has media but the overlay
              //      produces an empty URL on every frame).
              //      Throttle to once per second instead, so we
              //      get a steady "Python returned empty" signal
              //      that the user can correlate with the picked-
              //      message log to confirm the overlay isn't
              //      producing a URL.
              if (media && (media.key || media.url)) {
                if (media.key !== lastMediaKey) {
                  console.log(
                    `[preview-media] python returned: key=${media.key} kind=${media.kind} url=${media.url} opacity=${Number(media.opacity || 0).toFixed(2)}`,
                  );
                }
              } else {
                const now = Date.now();
                if (now - lastEmptyLogAt > 1000) {
                  const shape = media === null
                    ? "null"
                    : media === undefined
                      ? "undefined"
                      : `{key=${JSON.stringify(media.key)} url=${JSON.stringify(media.url)} kind=${JSON.stringify(media.kind)} opacity=${media.opacity}}`;
                  console.log(
                    "[preview-media] python returned empty: %s — image will NOT render this frame (no BrowserMediaOverlay active, or overlay returned empty url)",
                    shape,
                  );
                  lastEmptyLogAt = now;
                }
              }
            }
            applyMedia(media);
          }

          if (typeof window.get_frame_rgba === "function") {
            const bytes = window.get_frame_rgba();
            // `bytes` is a Pyodide-converted Uint8Array view; in plain
            // CPython (impossible here, but defensive) it would be a bytes
            // object. Render it as fuzzy LED circles (see blitFuzzy).
            const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
            blitFuzzy(ctx, view);
          }

          // Coordinator-state heartbeat: log mode + brightness once
          // per second so we can correlate what's on screen with the
          // fade ramp's values. The combination of mode + brightness
          // explains most "where did the text go?" regressions:
          // - `mode=hold & scroller_brightness=1.0` is the healthy
          //   full-text state.
          // - `mode=text_out & scroller_brightness≈0.5` means the
          //   coordinator is mid-fade-out after hold ended.
          // - `mode=in & scroller_brightness≈0.2` means the in-phase
          //   fade-in is partway through.
          // - `media_opacity=0` while mode=hold means the
          //   BrowserMediaOverlay hasn't been swapped in as `current`
          //   (the picked message's `media` list was empty, or the
          //   cycler was never constructed).
          if (PREVIEW_DEBUG && typeof window.get_diagnostics === "function") {
            const nowMs = Date.now();
            if (nowMs - lastDiagnosticsAt > 1000) {
              lastDiagnosticsAt = nowMs;
              let diag = {};
              try { diag = window.get_diagnostics(); } catch (e) { /* ignore */ }
              const key = `${diag.mode}|${diag.effect_name}|${diag.scroller_brightness}|${diag.media_opacity}|${diag.showing_text}|${diag.phase_elapsed}`;
              if (key !== lastDiagnosticsKey) {
                console.log(
                  `[preview-state] mode=${diag.mode || "?"} effect=${diag.effect_name || "?"} ` +
                  `scroller_b=${Number(diag.scroller_brightness || 0).toFixed(2)} ` +
                  `media_opacity=${Number(diag.media_opacity || 0).toFixed(2)} ` +
                  `showing_text=${diag.showing_text ? "yes" : "no"} ` +
                  `phase_elapsed=${Number(diag.phase_elapsed || 0).toFixed(1)}s ` +
                  `fade_progress=${Number(diag.fade_progress || 0).toFixed(2)} ` +
                  `text=${JSON.stringify(diag.scroller_text || "")}`,
                );
                lastDiagnosticsKey = key;
              }
            }
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
