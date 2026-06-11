## 1. ScrollerBase in lib_shared (shared by device and browser)

- [ ] 1.1 Create `lib_shared/scroller_base.py` with the `ScrollerBase` class — owns time/pixel logic, text state, top_x / bottom_x, top_y / bottom_y, single_line flag, frame_delay, offset_seconds, brightness. Provides `set_text`, `tick`, `render`, and three subclass hooks: `measure_text(text)`, `draw_text(canvas, text, x, y, color)`, `compute_layout(canvas_width, canvas_height)`
- [ ] 1.2 Add a unit test in `tests/scroller_base_test.py` that subclasses `ScrollerBase` with stub `measure_text` / `draw_text` / `compute_layout`, calls `set_text("hi", 64)`, ticks over a few frames, and asserts `top_x` decreases by the expected number of pixels and that `bottom_x` lags by `offset_seconds`

## 2. Refactor existing Scroller to MatrixScroller (heart-matrix-controller)

- [ ] 2.1 In `heart-matrix-controller/scroller.py`, rename the existing `Scroller` class to `MatrixScroller` and have it inherit from `ScrollerBase`. Keep the existing `rgbmatrix.graphics.Font` + `graphics.DrawText` logic, but move the time/pixel math into the base class
- [ ] 2.2 Update `heart-matrix-controller/main.py` to import and instantiate `MatrixScroller` instead of `Scroller` (one-line change)
- [ ] 2.3 Add a test in `tests/scroller_matrix_test.py` that constructs a `MatrixScroller` against a stub display, calls `set_text`, ticks, and asserts the same x-position math as the base-class test (proving the rename preserved behavior)

## 3. PreviewScroller in heart-message-manager (browser-side)

- [ ] 3.1 Create `heart-message-manager/preview_scroller.py` with a `PreviewScroller(ScrollerBase)` class. Loads a TTF font via `PIL.ImageFont.truetype` (configurable via `PREVIEW_FONT_PATH`, defaulting to Pillow's `load_default()`). Implements `measure_text` via `self.font.getbbox(text)[2]`. Implements `draw_text` via `PIL.ImageDraw.Draw(target).text((x, y), text, fill=color, font=self.font)` where `target = canvas.image if hasattr(canvas, "image") else canvas`
- [ ] 3.2 Add a test in `tests/scroller_preview_test.py` that constructs a `PreviewScroller`, calls `set_text`, ticks, and asserts the x-position math matches the base-class test. Also assert that the text appears at the expected baseline (top_y / bottom_y) for a 64×64 canvas

## 4. Effect cycle wiring (graceful skip in the browser)

- [ ] 4.1 In `heart-message-manager/preview_renderer.py` (browser-side), wrap each pattern's constructor in a try/except. If it raises, log a warning naming the pattern and the reason, and exclude it from the cycle. Patterns to handle: `Fireworks`, `Flame`, `NightSky`, `Honeycomb` (in-browser); `PngDisplay`, `VideoDisplay` (skipped in v1 — log and exclude without trying)
- [ ] 4.2 Add a test in `tests/preview_renderer_test.py` that constructs a `PreviewRenderer` with stubbed effect constructors (one raising) and asserts the cycle contains only the ones that initialized

## 5. WebDisplay and WebCanvas shim (browser-side Python)

- [ ] 5.1 Create `heart-message-manager/preview_canvas.py` with a `WebCanvas` class wrapping a Pillow `Image` (RGB mode, 64×64) and exposing `SetPixel(x, y, r, g, b)` and `SetImage(pil_image, x=0, y=0)` methods, plus a `to_imagedata()` that returns a JS `ImageData` via `pyodide.ffi.to_js`
- [ ] 5.2 Add a `WebDisplay` class wrapping a `WebCanvas` and exposing it as `.canvas` (so the patterns' `display.canvas.width/height` lookups work)
- [ ] 5.3 Add a test that asserts `SetPixel` writes the expected RGB to `image.getpixel((x, y))` and `SetImage` pastes at the requested origin. The `to_imagedata` test can be a thin smoke test that calls it and asserts the result is a JS proxy of the right length (64 × 64 × 4 = 16,384 bytes for RGBA)

## 6. PreviewCoordinator (browser-side, mirrors device's EffectCoordinator)

- [ ] 6.1 Implement a `PreviewCoordinator` in `heart-message-manager/preview_renderer.py` mirroring `heart-matrix-controller/main.py:EffectCoordinator` (fade_seconds, fade_step, gamma, idle/out/in mode machine, `request_message(text)`, `tick()`)
- [ ] 6.2 The coordinator's `tick()` SHALL advance the active effect via `Effect.tick` and clear/repaint the `WebCanvas` each iteration (effect renders into the canvas via `Effect.render(canvas)`)
- [ ] 6.3 Add a test that calls `request_message("hi")` and asserts the coordinator transitions `idle → out → in → idle` over the configured `fade_seconds`, and that the active effect index advances on the cycle boundary

## 7. PyScript setup

- [ ] 7.1 Add `heart-message-manager/py-config.toml` declaring the PyScript runtime and listing Pillow + numpy as packages
- [ ] 7.2 Add a `<py-config src="py-config.toml">` link tag in the head of `templates/preview.html`
- [ ] 7.3 Add a `<py-script src="/static/preview_main.py">` block (or inline `<py-script>`) that imports `rgb_display`, the patterns, `lib_shared.scroller_base`, `heart-message-manager.preview_scroller`, the shims, and instantiates the coordinator. The Python entry point SHALL expose a JS-callable function `coordinator.request_message(body)` via `pyodide.ffi.create_proxy` and a `coordinator.tick()` for the JS main loop to call
- [ ] 7.4 Ship the `heart-matrix-controller/rgb_display.py`, `heart-matrix-controller/patterns/*.py`, `lib_shared/scroller_base.py`, and `heart-message-manager/preview_scroller.py` as static files. Verify the import paths resolve in PyScript's working directory
- [ ] 7.5 Configure Flask's response headers to set `Content-Security-Policy` allowing `wasm-unsafe-eval` and the PyScript CDN's `script-src` (or self-host PyScript to avoid the CDN requirement)

## 8. UI — preview page (no WebSocket; polls /api/live-messages every 3 s)

- [ ] 8.1 Extend `heart-message-manager/templates/preview.html` with:
  - A `<canvas id="sign-canvas">` element sized to fit the viewport, capped at 800px, with `image-rendering: pixelated` CSS
  - A status block showing the current effect name and message body
  - A "Loading preview…" indicator that hides once PyScript signals ready
- [ ] 8.2 Add `static/preview.js` that:
  - On PyScript `pyodideReady` event, calls the Python entry point to start the coordinator
  - Starts a `requestAnimationFrame` loop, capped at 30 FPS (skip if `now - lastTick < 1000/30`), that calls `coordinator.tick()` and `ctx.putImageData(canvas.to_imagedata(), 0, 0)`
  - Starts a `setInterval(pollLatestMessage, 3000)` loop that mirrors `templates/testing.html`'s `setInterval(fetchMessages, 3000)`. `pollLatestMessage()` does `fetch('/api/live-messages?limit=1&suppress=true')`, reads the JSON, and compares the first message's `body` (or `null` if empty) to a `lastShownBody` module-scope variable. If they differ (or `lastShownBody` is `null` and the response is non-empty), it calls `coordinator.request_message(body)` via the PyScript interop and updates `lastShownBody`. If the response is an empty list, `lastShownBody` is left as-is and no `request_message` is called. The first invocation SHALL run immediately at startup, matching the testing page's `fetchMessages(); setInterval(fetchMessages, 3000);` pattern
  - Reads the effect name and current message body from the coordinator and updates the status block DOM
  - **Does NOT open a WebSocket or maintain any persistent push connection** — v1 uses polling only
- [ ] 8.3 Add a test in `tests/preview_template_test.py` that GETs `/preview` and asserts the response contains the canvas element, the status block, the `py-script` tag, and does NOT contain any WebSocket-related code (`new WebSocket` / `Flask-Sock` references)
- [ ] 8.4 Add a test in `tests/preview_poll_test.py` that asserts `static/preview.js`:
  - Contains a `setInterval(..., 3000)` call (or equivalent `3000` cadence) targeting a polling function
  - Fetches `/api/live-messages?limit=1&suppress=true` from the polling function
  - Compares the polled first message's `body` against a `lastShownBody` variable before calling `coordinator.request_message(body)`
  - Does not call `coordinator.request_message(body)` when the polled body is identical to `lastShownBody` (the dedup branch)

## 9. Verification

- [ ] 9.1 Run the full test suite (`PYTHONPATH=. pytest tests/ -v`) and confirm all new + existing tests pass
- [ ] 9.2 Start the Flask app locally, open `/preview` in Chrome and Firefox, confirm:
  - "Loading preview…" appears briefly, then the canvas animates
  - The canvas updates at ≥ 30 FPS (DevTools Performance tab)
  - At least one effect (fireworks / flame / nightsky / honeycomb) animates correctly
  - The status block updates with the current effect and message
- [ ] 9.3 Trigger an SMS via the local curl test in the project README. Within at most 3 s (one poll interval) the preview SHALL observe the new body, call `coordinator.request_message(body)`, and the canvas SHALL begin scrolling the new message — **no page refresh required** (the polling loop picks it up). Repeat the trigger and confirm the second new message also appears within 3 s
- [ ] 9.4 Switch to a different tab for 30 s, switch back, confirm the polling loop and the render loop both resumed (browser throttles `setInterval` and pauses `requestAnimationFrame` while the tab is hidden)
- [ ] 9.5 Open `/preview` in 5 tabs simultaneously, trigger a single SMS via curl, and confirm all 5 previews cycle to the new message within 3 s (each tab's independent poll picks it up on its next tick). Confirm the Flask process's CPU does not increase proportionally to tab count — the polling cost matches what the existing `/testing` page already produces (use `htop` or `top`)
- [ ] 9.6 Confirm the Flask process no longer has any per-client render loop (a `grep -r "PreviewCoordinator\|PreviewRenderer" heart-message-manager/main.py` should be empty; the coordinator and renderer are browser-side only)
- [ ] 9.7 Confirm `scroller.py` from `heart-matrix-controller/` is not imported anywhere in the preview's browser path (the browser loads only `rgb_display.py`, the patterns, `lib_shared/scroller_base.py`, and `heart-message-manager/preview_scroller.py`)
- [ ] 9.8 Confirm `rgbmatrix` is not imported in the browser path (`grep -r "rgbmatrix" heart-message-manager/preview_*.py heart-message-manager/preview_main.py` should be empty)
- [ ] 9.9 Confirm the device's `MatrixScroller` produces the same x-position behavior as the browser's `PreviewScroller` over a fixed elapsed time (cross-implementation alignment test — could be a one-off script rather than a CI test)
- [ ] 9.10 Confirm there is no `PreviewBroadcaster` and no `WS /api/preview/stream` route in the Flask app (the v1 design has neither; both are explicitly deferred to a future revision)
- [ ] 9.11 Confirm the existing `MessageManager` and `/api/live-messages` wiring is unchanged: `grep -nE "MessageManager\\(|on_message=|_on_message|broadcaster|callback" heart-message-manager/main.py` SHALL match only the pre-existing call sites (the no-callback `MessageManager()` instantiation at module load, the `make_mqtt_client(_message_mgr.dispatch)` wiring, the existing route handler at `/api/live-messages`, and unrelated auth tests) — no new callback, no broadcaster, no `_on_message` parameter is added
- [ ] 9.12 Confirm `templates/testing.html` and `tests/test_auth.py` (the other in-app consumers of `/api/live-messages` and `/api/live-messages/seed`) are unchanged by this change (the existing `fetch('/api/live-messages?suppress=' + ...)` and `client.get("/api/live-messages")` calls keep working without modification)
- [ ] 9.13 Confirm the response shape of `/api/live-messages` is unchanged: `git diff` on `heart-message-manager/main.py`'s `/api/live-messages` route handler and on `lib_shared/message_manager.py` is empty for this change
