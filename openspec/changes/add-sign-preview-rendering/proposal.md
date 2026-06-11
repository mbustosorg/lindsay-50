## Why

The admin UI has a "Preview" tab placeholder but it doesn't render anything yet. Operators have no way to see what the sign will actually *look like* — the scrolling text, the cycling background effect, the brightness, the colors. When configuring filters, allowed senders, or rendering settings, there's no feedback loop that confirms the visual result matches what's about to ship to the LED panel. This change makes the existing `heart-matrix-controller` rendering code (effects + scroller) runnable in the web browser so operators can see exactly what the sign will display.

Critically, the preview runs **in the browser** (via PyScript / WASM Python) rather than on the Flask server, so the cost of an open preview tab is independent of how many operators or tabs are connected. The server stays a thin static-files + small JSON host; the heavy lifting happens per-tab in the user's browser.

In v1 the preview shows the **most recent filtered message** and stays in sync with the Flask `MessageManager` by **polling `/api/live-messages?limit=1&suppress=true` every 3 seconds** (the same cadence and pattern `templates/testing.html` uses for its `setInterval(fetchMessages, 3000)` loop). When the polled body differs from the last one shown, the browser hands the new body to the coordinator via `coordinator.request_message(body)`. If the response is an empty list, the coordinator keeps its current state. The preview does **not** open any WebSocket or MQTT push channel in v1 — that path is parked for a future revision once the message-rotation algorithm is settled.

## What Changes

- Build out the `/preview` page in the admin UI to render the sign's live visual output
- Run the existing `Bitmap` / `Palette` / `Effect` / pattern code from `heart-matrix-controller/` **in the browser** via PyScript, unmodified
- Factor scroller time/pixel logic into a new `lib_shared/scroller_base.py` so the device and the browser share one implementation. The existing `Scroller` in `heart-matrix-controller/scroller.py` becomes `MatrixScroller(ScrollerBase)`; a new `PreviewScroller(ScrollerBase)` in `heart-message-manager/preview_scroller.py` runs in the browser using Pillow's font rendering
- Add a thin `WebDisplay` + `WebCanvas` shim (Python) that exposes the `SetPixel` / `SetImage` API the effect code expects, backed by a Pillow image that the browser blits to an HTML5 `<canvas>` once per frame
- The 64×64 panel renders to a scaled `<canvas>` (capped at 800px wide), driven by `requestAnimationFrame` and capped at the device's 30 FPS
- All four effects that work in the browser unchanged (fireworks, flame, nightsky, honeycomb); `png_display` and `video_display` are gracefully skipped in v1
- A "Current effect" indicator shows which background is active, matching the cycle order on the device
- A "Now displaying" indicator shows the current message body and the fade-in / fade-out transitions
- After PyScript is ready, the browser starts a `setInterval` loop that **polls `/api/live-messages?limit=1&suppress=true` every 3 s** (the same shape `templates/testing.html` uses). When the polled body differs from the last body the coordinator was handed, the browser calls `coordinator.request_message(body)`. The first successful poll also seeds the initial state on page load. If the response is an empty list, the coordinator keeps its current state (no `request_message` call, no effect cycle). The preview does **not** open a WebSocket — no persistent push channel, no reconnect logic

## Capabilities

### New Capabilities

- `sign-preview-rendering`: Web-based live simulation of the 64x64 LED panel that runs the device's unmodified effect and scroller code in the browser via PyScript, paints frames to an HTML5 canvas sized to the available viewport (capped at 800px wide), and animates at the device's frame cadence so operators see exactly what the sign will render. The browser stays in sync with the Flask `MessageManager` by polling `/api/live-messages?limit=1&suppress=true` every 3 seconds (mirroring the cadence `templates/testing.html` uses) and calls `coordinator.request_message(body)` whenever a new body is observed; no WebSocket or MQTT push channel is used in v1.

### Modified Capabilities

*(none — the existing `/preview` page is a placeholder with no behavior to modify. The visual canvas is a new capability layered onto the page; the eventual filtered-list behavior is covered by the still-open `flask-management-app` change.)*

## Impact

- New files:
  - `lib_shared/scroller_base.py` — `ScrollerBase` (time/pixel logic, text state, brightness); shared by both environments
  - `heart-message-manager/preview_canvas.py` — `WebDisplay` and `WebCanvas` shims (run in the browser via PyScript)
  - `heart-message-manager/preview_scroller.py` — `PreviewScroller(ScrollerBase)` with Pillow `ImageFont.truetype` font rendering
  - `heart-message-manager/preview_renderer.py` — `PreviewCoordinator` (browser-side, mirrors `EffectCoordinator` from `heart-matrix-controller/main.py`)
  - `heart-message-manager/py-config.toml` — PyScript config (Pillow + numpy)
  - `heart-message-manager/templates/preview.html` (extended) — adds `<py-script>` block, `<canvas id="sign-canvas">`, and status block
  - `heart-message-manager/static/preview.js` — bootstraps the main loop, draws to the canvas, fetches the latest message on load
- Modified:
  - `heart-matrix-controller/scroller.py` — rename `Scroller` to `MatrixScroller(ScrollerBase)`; same `rgbmatrix.graphics.Font` + `graphics.DrawText` calls, but the time/pixel logic now lives in the base class
  - `heart-matrix-controller/main.py` — one-line change: `Scroller(display)` → `MatrixScroller(display)`
  - CSP / response headers — allow `wasm-unsafe-eval` and the PyScript CDN's `script-src`
- **No** Flask-side WebSocket endpoint, **no** `PreviewBroadcaster`, **no** `Flask-Sock` dependency — v1 has no live push; these are deferred to a future revision once the message-rotation algorithm is settled
- New browser-side dependencies (loaded by PyScript, cached after first load):
  - Pyodide runtime (~6.5 MB)
  - Pillow (~500 KB)
  - numpy (~3 MB)
  - **Total first-load: ~10 MB.** Cached by the browser.
- No changes to `heart-matrix-controller/` effect modules — the patterns are imported by the browser as-is
- The Flask process no longer has a `PreviewCoordinator`; the only new server-side concern is shipping the static assets (the existing `/api/live-messages` endpoint is reused as-is)
- The issue notes this may "ultimately move into lib_shared" — per the issue, **no existing files are moved**. The one new file in `lib_shared/` (the scroller base class) is *new* code, not a move. Reuse comes from the base class's shared inheritance, not from relocating existing files.
