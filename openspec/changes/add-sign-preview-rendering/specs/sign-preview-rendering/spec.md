## ADDED Requirements

### Requirement: Web preview renders the LED panel output

The admin UI SHALL provide a page that renders a live, in-browser simulation of the 64×64 LED panel driven by the same effect code that the device runs. The page SHALL show a canvas representing the panel, scaled to the available viewport width and capped at 800px, updated continuously at ≥ 30 FPS.

The rendering SHALL happen **in the browser** (via PyScript / WASM Python), not on the Flask server. The Flask process SHALL NOT maintain a per-client render loop or push frames to the browser; it SHALL serve static assets only. The cost of an open preview tab SHALL be independent of the number of operators connected.

#### Scenario: Preview page loads with current effect cycling
- **WHEN** a user visits the preview page and no message has been received in the current session
- **THEN** the canvas animates with the currently-active background effect from the device's effect cycle (one of fireworks, flame, nightsky, or honeycomb — `png_display` and `video_display` are excluded in v1) and the canvas dimensions are `64 * N` × `64 * N` where N is the largest integer that fits the viewport width up to `800 / 64 = 12`

#### Scenario: No message body yields idle state
- **WHEN** the preview's scroller has no text to display
- **THEN** only the background effect is rendered, and no scrolling text appears on the canvas

#### Scenario: Server CPU is independent of the number of connected preview tabs
- **WHEN** N preview tabs are open simultaneously
- **THEN** the Flask process SHALL NOT increase its per-frame CPU or memory proportionally to N; the only server-side work per tab load is 1 small JSON request to fetch the latest message

### Requirement: Preview runs the device's effect and scroller code in the browser via PyScript

The preview SHALL run the actual unmodified Python effect code from `heart-matrix-controller/` and the shared scroller logic from `lib_shared/scroller_base.py` in the browser via PyScript (or Pyodide directly). The effect modules and the shared scroller base SHALL NOT be forked, copied, transpiled, or reimplemented in a different language. The server SHALL ship these files as static assets; the browser SHALL import them at runtime.

The device's `Scroller` class uses `rgbmatrix.graphics.DrawText` and cannot be loaded in the browser, so the device's `Scroller` is renamed to `MatrixScroller(ScrollerBase)` and stays in `heart-matrix-controller/scroller.py`. The browser loads a new `PreviewScroller(ScrollerBase)` in `heart-message-manager/preview_scroller.py` that uses Pillow's `ImageFont.truetype` to render glyphs. Both subclasses share the same time/pixel logic via the base class, so scroll speed, two-line offset, and tick math are identical.

#### Scenario: rgbmatrix is not imported in the browser render path
- **WHEN** the preview initializes in the browser
- **THEN** `heart-matrix-controller/scroller.py` (the `MatrixScroller` that uses `rgbmatrix.graphics`) is not loaded; the browser loads only `lib_shared/scroller_base.py` and `heart-message-manager/preview_scroller.py`

#### Scenario: All in-browser-capable effects are exercised by the preview
- **WHEN** the preview's coordinator cycles through its effect list
- **THEN** each of fireworks, flame, nightsky, and honeycomb SHALL initialize successfully in the browser and animate normally

#### Scenario: Effects that fail to initialize are skipped gracefully
- **WHEN** an effect's initializer raises in the browser (e.g. missing assets, missing optional deps)
- **THEN** that effect is logged and excluded from the preview's effect cycle; the remaining effects continue to render normally

#### Scenario: `png_display` and `video_display` are excluded in v1
- **WHEN** the preview initializes
- **THEN** `PngDisplay` and `VideoDisplay` SHALL be excluded from the effect cycle (logged as skipped), even if they would otherwise work in the browser

#### Scenario: Scroller tick math is identical between device and browser
- **WHEN** a `set_text` is dispatched to either the device's `MatrixScroller` or the browser's `PreviewScroller` with the same text, the same `frame_delay`, and the same `offset_seconds`
- **THEN** after the same elapsed wall-clock time, `top_x` SHALL have decreased by the same number of pixels in both implementations, and `bottom_x` SHALL lag by the same offset

### Requirement: WebDisplay and WebCanvas shim exposes the rgbmatrix Canvas API the effects expect

A `WebDisplay` and `WebCanvas` shim SHALL be provided, runnable in PyScript, that exposes the methods the patterns' constructors and the `Effect.render` base method call:

- `display.canvas.width` and `display.canvas.height` (read by the patterns' `tick` methods)
- `canvas.SetPixel(x, y, r, g, b)` (called by `Effect.render`)
- `canvas.SetImage(pil_image, x=0, y=0)` (called by full-color effects' overridden `render`)

The shim SHALL be backed by a Pillow `Image` in the browser. A `to_imagedata()` method SHALL convert the Pillow image to a JS `ImageData` so the browser can blit it to the HTML5 canvas once per frame.

#### Scenario: WebCanvas is the only object passed to effect constructors
- **WHEN** a pattern is constructed in the browser
- **THEN** it receives a `WebDisplay` instance whose `.canvas` is a `WebCanvas` instance; the pattern's `Effect.render(canvas)` call dispatches `SetPixel` / `SetImage` on the `WebCanvas` and the pixels land in the underlying Pillow image

#### Scenario: Frame blit updates the visible canvas
- **WHEN** the browser's main loop calls `to_imagedata()` followed by `ctx.putImageData(...)`
- **THEN** the HTML5 canvas SHALL reflect the new frame and the previous frame SHALL be replaced

### Requirement: Browser-driven main loop matches the device's frame cadence

The preview's main loop SHALL be driven by `requestAnimationFrame` in the browser, capped at ≥ 30 FPS to match the device's native cadence. Running the loop faster than 30 FPS SHALL be avoided so the preview doesn't appear smoother than the sign (which could mislead operators about what the device will show). When the tab is hidden, `requestAnimationFrame` SHALL automatically pause, releasing the CPU.

#### Scenario: Render loop runs at the device's frame cadence
- **WHEN** the preview page is visible and the coordinator is running
- **THEN** the canvas SHALL be updated at least 30 times per second

#### Scenario: Hidden tab pauses the render loop
- **WHEN** the user switches to a different tab
- **THEN** `requestAnimationFrame` SHALL pause and the browser SHALL stop drawing frames; switching back resumes the loop

#### Scenario: First-load is bounded
- **WHEN** a user first opens the preview page
- **THEN** the page SHALL show a "Loading preview…" indicator and the canvas SHALL appear within 30 s on a typical broadband connection; subsequent loads (browser-cached) SHALL show the canvas within 5 s

### Requirement: Preview polls `/api/live-messages` every 3 s and shows new messages

After PyScript has finished initializing in the browser, the preview SHALL start a polling loop that fetches `/api/live-messages?limit=1&suppress=true` every 3 seconds — the same cadence and pattern `templates/testing.html` uses for its `setInterval(fetchMessages, 3000)` loop. The browser SHALL track the most recently handed-off body in module-scope state (a `lastShownBody` variable). On every poll, the browser SHALL compare the polled first message's `body` to `lastShownBody`; if they differ (or if the response is non-empty and `lastShownBody` is `null`), the browser SHALL call `coordinator.request_message(body)` via the PyScript interop and update `lastShownBody`. If the polled list is empty, the coordinator SHALL keep its current state (no `request_message` call, no effect cycle).

The 3-second polling cadence is the same shape the existing testing page already uses, so the per-tab server load is not new; the preview does not open a WebSocket or any persistent push channel. A future revision may replace polling with a live channel (WebSocket / MQTT push) or a client-side rotation algorithm over a fetched set, depending on the shape of the message-rotation algorithm that lands. The coordinator's `request_message(body)` API is stable across all of these.

#### Scenario: First poll seeds the initial state
- **WHEN** the preview page loads and the Flask MessageManager has at least one filtered message
- **THEN** the first poll SHALL fetch `/api/live-messages?limit=1&suppress=true`, take the first message's `body`, and call `coordinator.request_message(body)` — the coordinator SHALL fade the active effect out, advance to the next effect in the cycle, fade it back in, and start scrolling the new message body

#### Scenario: A new SMS arrival is reflected within 3 s
- **WHEN** the preview page is open, a new SMS arrives via Twilio, the Flask MessageManager dispatches it, and the MessageManager's ring buffer now ends with a new body
- **THEN** within at most 3 s (one poll interval) the browser SHALL observe the new body, call `coordinator.request_message(body)`, and the canvas SHALL begin scrolling the new message

#### Scenario: Duplicate poll response is a no-op
- **WHEN** two consecutive polls return the same first-message body (no new message has arrived)
- **THEN** the browser SHALL NOT call `coordinator.request_message(body)` on the second poll; the coordinator's state (active effect, scroller text, current cycle position) SHALL be unchanged

#### Scenario: Empty polled list yields no coordinator call
- **WHEN** the Flask MessageManager has no non-suppressed messages
- **THEN** the polled response SHALL be an empty list and the browser SHALL NOT call `coordinator.request_message`; the coordinator SHALL keep its current state

#### Scenario: Suppressed messages are not shown
- **WHEN** the most recent message in the Flask MessageManager is suppressed (filtered out)
- **THEN** `?suppress=true` SHALL exclude it; the browser SHALL receive the most recent non-suppressed message (or an empty list if all recent messages are suppressed)

#### Scenario: Hidden tab throttles polling
- **WHEN** the user switches to a different browser tab
- **THEN** the browser's `setInterval` SHALL be throttled (typically to 1 Hz or less), reducing server load while the tab is not visible; switching back to the preview tab SHALL resume normal polling at the 3 s cadence

#### Scenario: Polling does not open a WebSocket
- **WHEN** the preview page is open
- **THEN** no `WebSocket` object SHALL be instantiated by `static/preview.js`; the only network activity is the recurring `fetch('/api/live-messages?limit=1&suppress=true')`

### Requirement: Preview exposes current effect name and current message body

The preview page SHALL display, alongside the canvas, the name of the currently-active background effect and the body of the message currently being scrolled (or an "Idle" indicator when no message is being scrolled). Both values SHALL update in step with the canvas.

#### Scenario: Effect name updates on cycle
- **WHEN** the preview's EffectCoordinator advances to the next effect in its cycle
- **THEN** the displayed effect name updates to match the new active effect

#### Scenario: Message body updates when polling observes a new body
- **WHEN** a poll observes a new first-message body different from the last body the coordinator was handed
- **THEN** the displayed message body updates to the new message's body text (decoded as UTF-8) once the scroller begins showing it
