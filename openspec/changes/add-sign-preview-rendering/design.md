## Context

The admin UI has a `/preview` tab placeholder that doesn't render anything. Operators configuring filters, senders, or rendering settings have no way to see what the device will display.

The rendering stack already exists in `heart-matrix-controller/`:

- `rgb_display.py` — `Bitmap`, `Palette`, `Effect` (base class with palette-based brightness fade + `render(canvas)`). **Imports only `lib_shared.config_reader`.** No `rgbmatrix` dependency.
- `scroller.py` — `Scroller` with `set_text` / `tick` / `render(canvas)`; loads a BDF font via `rgbmatrix.graphics.Font`. **The only file in the rendering stack that imports `rgbmatrix`.**
- `patterns/*.py` — six effects (fireworks, flame, nightsky, png_display, video_display, honeycomb). All import from `rgb_display` and (sometimes) `lib_shared` only. No `rgbmatrix` import.
- `main.py` — `EffectCoordinator` cycles effects, fades brightness in/out, drives the scroller

The display's `render(canvas)` API is `canvas.SetPixel(x, y, r, g, b)` plus `canvas.SetImage(pil_image)` for full-color effects. The hzeller `rgbmatrix` `RGBMatrix.Canvas` happens to expose both. A small adapter (in JS or in Python via PyScript) can expose the same methods backed by an HTML5 `<canvas>`.

The web app (`heart-message-manager/`) and the device (`heart-matrix-controller/`) share `lib_shared/` and the existing `MessageManager`. The Flask process subscribes to the same MQTT feed the device does and maintains its own live message ring buffer — the preview reuses the existing `/api/live-messages` endpoint to read the current message list and stays in sync with the MessageManager by polling that endpoint every 3 s, the same cadence and pattern `templates/testing.html` uses for its `setInterval(fetchMessages, 3000)` loop (no new endpoint, no WebSocket, no callback wiring; see Decision 5).

**A note on `lib_shared/`.** The original issue said "this may ultimately move into lib_shared, but please don't move files around for now." We're honoring that — the existing `heart-matrix-controller/` files stay where they are. The one deliberate exception is the new `lib_shared/scroller_base.py` (see Decision 3), which is *new code* rather than a move; it factors out scroller math so the device and the browser can share it. Everything else in `heart-matrix-controller/` (effect code, `Scroller`'s rgbmatrix glue) stays put.

## Goals / Non-Goals

**Goals:**

- Web users see a live, animated simulation of what the 64×64 LED panel will display
- Reuse the existing effect code AND the scroller's pixel/timing logic unchanged — no forks, no parallel implementations, no transpilation
- **Server CPU cost is independent of the number of open preview tabs** — the heavy lifting happens in the browser
- Hit the device's native frame cadence (≥ 30 FPS) so the preview is visually indistinguishable in motion from the sign
- The browser stays in sync with the Flask `MessageManager` by **polling `/api/live-messages?limit=1&suppress=true` every 3 s** (the same cadence `templates/testing.html` uses) and shows the latest filtered message being scrolled across the panel — a new body observed during polling is handed to the coordinator via `coordinator.request_message(body)`
- The 64×64 panel scales to the available viewport width, capped at 800px, with nearest-neighbor scaling (LEDs are discrete pixels, no smoothing)
- The preview page also surfaces: the currently active effect, the message body being scrolled, and a "no message" idle state

**Non-Goals:**

- Moving any existing file from `heart-matrix-controller/` into `lib_shared/`. (One new file in `lib_shared/` — the scroller base class — is added; see Decision 3.)
- Reproducing the *exact* BDF glyphs the device uses — a close-enough proportional font at the same pixel size is fine
- A new effect — only the six existing effects are simulated
- Historical message playback (the algorithm will be reworked separately to leverage historical messages; the preview shows *now*)
- Forcing a particular effect from the UI — the future Settings/Playlists surface will own effect selection
- Per-pixel pixel-perfect match between preview and device (see "Why the preview is not a perfect mirror" below)
- **Changing the existing `MessageManager` wiring or the `/api/live-messages` endpoint.** The preview is a *read-only consumer* of the existing path. The `_message_mgr = MessageManager()` instantiation in `heart-message-manager/main.py` stays as-is (no callback). The MQTT client is still constructed via `make_mqtt_client(_message_mgr.dispatch)`. The `/api/live-messages` route, the `/api/live-messages/seed` route, the `_message_mgr.get_messages(limit, suppress)` contract, and the response shape `[m.to_dict() for m in ...]` are preserved exactly — `templates/testing.html` and `tests/test_auth.py` (which call this endpoint) keep working unchanged. The preview's JS does a plain `fetch('/api/live-messages?limit=1&suppress=true')` on a `setInterval(..., 3000)` loop (mirroring `templates/testing.html`'s pattern); it does not add a new endpoint, a WebSocket, a SSE stream, a callback, or any other new ingest surface.

## Decisions

### Decision 1: Client-side rendering via PyScript (Python in the browser)

The effect code is Python. We run it in the browser via **PyScript** (a framework on top of Pyodide / WASM Python) so the actual unmodified effect modules from `heart-matrix-controller/` execute in the user's tab. The Flask server ships only static files and a small JSON endpoint — no render loop, no per-frame network traffic, no scaling problem.

```
   ┌────────────────── Browser tab ──────────────────┐
   │                                                 │
   │  ┌── PyScript (WASM Python) ──┐                 │
   │  │  rgb_display.py            │                 │
   │  │  patterns/fireworks.py     │  ── tick() ──▶  │
   │  │  patterns/flame.py         │                 │
   │  │  patterns/nightsky.py      │  WebCanvas      │
   │  │  ...                       │  (PIL Image ──▶ │
   │  │  PreviewCoordinator        │   ImageData)    │
   │  │  WebDisplay / WebCanvas    │       │         │
   │  └────────────────────────────┘       │         │
   │                                       ▼         │
   │  ┌── <canvas id="sign-canvas"> ──────────┐      │
   │  │  drawImage on each requestAnimation-  │      │
   │  │  Frame tick (capped at device FPS)    │      │
   │  └───────────────────────────────────────┘      │
   │                                                 │
   │  ┌── preview.js ─────────────────────────┐      │
   │  │  bootstraps PyScript                  │      │
   │  │  setInterval(pollLatestMessage, 3000) │      │
   │  │  hands new body to coordinator (dedup) │      │
   │  │  drives the main loop, status block   │      │
   │  └───────────────────────────────────────┘      │
   └─────────────────────────────────────────────────┘
                       ▲           ▲
                       │           │ (static, cached)
                       │           │
            ┌──────────┴───┐   ┌───┴─────────────┐
            │ Flask server │   │ static assets   │
            │  (no render  │   │ (PyScript,      │
            │   loop, no   │   │  effects,       │
            │   WS, no     │   │  fonts, JS)     │
            │   polling)   │   │                 │
            └──────────────┘   └─────────────────┘
```

**The Flask process has no render loop and no per-client message channel.** It serves static files and reuses the existing `/api/live-messages` endpoint to give the browser its initial state. CPU per connected client ≈ zero. The device's exact Python effect code runs in the browser unchanged.

**Why PyScript over alternatives:**

- **vs. server-side rendering (the prior approach):** Linear scaling with clients, GPU/CPU contention on shared dynos, network round-trip per frame. Rejected for the reason you raised: many tabs brings the server to its knees.
- **vs. Python → JS transpile (Transcrypt, etc.):** Transpiles the effect code to JS at build time. Runs in the browser at 60+ FPS. Downside: a build step that maps `Bitmap`/`Palette`/`PIL` to JS, debugging transpiled code, and the *device* still runs the original Python — so we'd be maintaining two execution surfaces anyway. The transpile step is another place to drift. PyScript avoids the build step and avoids the second execution surface.
- **vs. full JS port:** Same as transpile but hand-written. Every effect change requires a parallel JS port. Rejected.
- **vs. raw Pyodide (skip PyScript):** Pyodide is the runtime; PyScript is the integration. PyScript gives us `<py-script>` blocks, declarative config, automatic dependency resolution. We could drop down to raw Pyodide if needed, but PyScript is the higher-leverage default.

**Trade-offs to acknowledge:**

- **Initial bundle size:** ~10 MB (Pyodide runtime + Pillow + numpy). Downloaded once, cached aggressively. For a preview page opened by an operator a few times a week, this is fine. For always-on dashboards, also fine (cached).
- **Cold start:** 5–10 s on first load (downloading + initializing WASM). Cached loads: 1–2 s. Mitigation: show a "Loading preview…" spinner; lazy-init PyScript only when the user opens `/preview`.
- **Browser quirks:** PyScript is well-tested on Chrome/Firefox/Safari but can be flaky on older browsers. Acceptable for an operator-facing preview.
- **PIL + numpy in WASM:** Both available; PIL image operations are fast (compiled to WASM). Full-color effects (`SetImage(pil_image)`) work unchanged.

### Decision 2: WebDisplay + WebCanvas shim — Python wraps a JS canvas

The effect modules assume a `display.canvas` object with `width`, `height`, `SetPixel(x, y, r, g, b)`, and `SetImage(pil_image, x, y)`. The patterns' constructors store `self.display = display` and read `self.display.canvas.width/height` per tick; the `Effect.render(canvas)` method calls `canvas.SetPixel`.

We provide a thin Python shim that the PyScript runtime instantiates once and hands to every pattern's `__init__`. The shim is a normal Python class:

```python
# preview_canvas.py — runs in the browser
from PIL import Image
from pyodide.ffi import to_js

class WebCanvas:
    """Pillow-backed canvas exposing the rgbmatrix Canvas subset the
    effects use. Lives in the browser; blits to <canvas> once per frame."""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.image = Image.new("RGB", (width, height))

    def SetPixel(self, x, y, r, g, b):
        self.image.putpixel((x, y), (r, g, b))

    def SetImage(self, pil_image, x=0, y=0):
        self.image.paste(pil_image, (x, y))

    def to_imagedata(self):
        # Convert PIL RGB to ImageData (RGBA Uint8ClampedArray)
        rgba = self.image.convert("RGBA")
        return to_js(rgba.tobytes())


class WebDisplay:
    """Adapter so the patterns' `display.canvas.width/height` lookups work."""

    def __init__(self, canvas):
        self.canvas = canvas
```

After the effect render, the JS-side main loop converts `canvas.to_imagedata()` → `new ImageData(...)` → `ctx.putImageData`. The "JS wrapper that mimics the rgbmatrix" is just an HTML5 `<canvas>` plus this thin Python shim that funnels pixels to it via `putImageData` once per frame.

**Why not let Python call into a JS-side `RGBMatrix` class with `SetPixel`/`SetImage`?** You could — PyScript's `create_proxy` lets Python call arbitrary JS objects. But the per-pixel `putpixel` from Python into a Python-side buffer is fast (a few microseconds per pixel for a 64×64 frame = ~250 µs total), and the JS bridge adds ~1–10 µs per call. Doing the buffer work in Python and blitting once at the end is simpler and equally fast.

**Compatibility with the effect modules:** The patterns only access `display.canvas.width` and `display.canvas.height` plus the `SetPixel` / `SetImage` calls inside `Effect.render`. Our shim exposes both. No pattern modifications needed.

**Compatibility with the device's `Scroller`:** The device's `Scroller` (now `MatrixScroller`, see Decision 3) calls `rgbmatrix.graphics.DrawText`, which we cannot import in the browser. The browser loads `lib_shared/scroller_base.py` (the time/pixel logic) and `heart-message-manager/preview_scroller.py` (the Pillow-based subclass); the rgbmatrix-using `MatrixScroller` itself is not loaded.

### Decision 3: Factor scroller time/pixel logic into `lib_shared`; subclass for each environment

The device's `Scroller` is the only file in the rendering stack that imports `rgbmatrix`. We don't want to fork it, transpile it, or hand-port it to JS. Instead we factor out the parts that *aren't* font-specific — the time-based math, the text state, the x/y position tracking, the brightness handling — into a new `ScrollerBase` in `lib_shared/`, and keep the font-specific pieces in environment-specific subclasses.

This way the same `tick()` math runs on the Pi (`MatrixScroller`, BDF via `rgbmatrix.graphics.Font`) and in the browser via PyScript (`PreviewScroller`, TTF via `PIL.ImageFont.truetype`). The two implementations stay aligned by construction — text positioning, scroll speeds, and two-line offset are all defined once in the base class.

**New file: `lib_shared/scroller_base.py`**

```python
class ScrollerBase:
    """Scroller time/pixel logic shared by MatrixScroller (rgbmatrix) and
    PreviewScroller (Pillow). Subclasses implement font loading and drawing."""

    def __init__(self, frame_delay=0.04, offset_seconds=1.0, color=0xFF0000):
        self.frame_delay = frame_delay
        self.offset_seconds = offset_seconds
        self.text = ""
        self.text_width = 0
        self.start_time = 0.0
        self.last_frame = 0.0
        self.top_x = 0
        self.bottom_x = 0
        self.single_line = False
        self.top_y = 0
        self.bottom_y = 0
        self._color = color
        self._brightness = 1.0
        # Subclass must populate after font load:
        #   self.font, self.font_height, self.font_baseline

    def set_brightness(self, b):
        self._brightness = b

    def color_tuple(self):
        c, b = self._color, self._brightness
        return (
            int(((c >> 16) & 0xFF) * b),
            int(((c >> 8) & 0xFF) * b),
            int((c & 0xFF) * b),
        )

    def set_text(self, text, canvas_width):
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        self.text = str(text)
        self.text_width = self.measure_text(self.text)
        self.top_x = canvas_width
        self.bottom_x = canvas_width
        now = time.monotonic()
        self.start_time = now
        self.last_frame = now
        log.debug("New scroll text: %r", self.text)

    def tick(self, canvas_width):
        if not self.text:
            return
        now = time.monotonic()
        elapsed = now - self.last_frame
        if elapsed < self.frame_delay:
            return
        pixels = int(elapsed / self.frame_delay)
        self.last_frame += pixels * self.frame_delay
        end_x = -self.text_width
        new_top = self.top_x - pixels
        if new_top < end_x:
            new_top = canvas_width
        self.top_x = new_top
        if not self.single_line and now - self.start_time >= self.offset_seconds:
            new_bot = self.bottom_x - pixels
            if new_bot < end_x:
                new_bot = canvas_width
            self.bottom_x = new_bot

    def render(self, canvas):
        if not self.text:
            return
        color = self.color_tuple()
        self.draw_text(canvas, self.text, self.top_x, self.top_y, color)
        if not self.single_line:
            self.draw_text(canvas, self.text, self.bottom_x, self.bottom_y, color)

    # --- Subclass hooks ---

    def measure_text(self, text):
        """Subclass: return the pixel width of `text` in self.font."""
        raise NotImplementedError

    def draw_text(self, canvas, text, x, y, color):
        """Subclass: blit `text` at (x, y) in self.font using the given color."""
        raise NotImplementedError

    def compute_layout(self, canvas_width, canvas_height):
        """Subclass: set self.single_line, self.top_y, self.bottom_y, and
        initial x positions from the canvas size and font metrics."""
        raise NotImplementedError
```

**`heart-matrix-controller/scroller.py` (modified):** existing `Scroller` is renamed to `MatrixScroller(ScrollerBase)`. Same `rgbmatrix.graphics.Font` + `graphics.DrawText` calls, but the time/pixel logic now lives in the base class. One-line constructor change in `main.py` (`Scroller(display)` → `MatrixScroller(display)`).

**`heart-message-manager/preview_scroller.py` (new):** `PreviewScroller(ScrollerBase)`. Loads a TrueType font via `PIL.ImageFont.truetype` (configurable via `PREVIEW_FONT_PATH`, defaulting to Pillow's `load_default()` if no TTF configured). `measure_text` calls `self.font.getbbox(text)[2]`; `draw_text` uses `PIL.ImageDraw.Draw(canvas.image if hasattr(canvas, "image") else canvas).text((x, y), text, fill=color, font=self.font)`.

**Why this beats a JS port:**

- **No parallel implementation.** All scroller math lives in one Python file. The Pi and the browser both run the same `tick()` and `set_text()`. Any change to scroll speed, two-line offset, or fade behavior is a one-file change in `lib_shared/`.
- **No transpile, no JS interop for the scroller.** It's pure Python, called from PyScript the same way as the effect code.
- **Glyph rendering is the only environment-specific concern.** Font load, font metrics, glyph blit — those are exactly what the subclass hooks are for. The text *appearance* will differ (BDF vs TTF) but the *behavior* (when, where, how fast) is identical.

**Why not just keep the existing `Scroller` and write `PreviewScroller` from scratch?** Because then any future tweak to scroll math (e.g. easing, character spacing) would have to be re-applied in two files, and they'd drift. The base class makes drift impossible — there's literally one definition of `tick()`.

**Note on scope:** the issue said "don't move files around." We're not moving `scroller.py`; it stays in `heart-matrix-controller/`. We're adding one new file in `lib_shared/` (the base class) and one new file in `heart-message-manager/` (the preview subclass). The Pi's `main.py` needs a one-line change (`Scroller` → `MatrixScroller`).

### Decision 4: Main loop driven by `requestAnimationFrame` in the browser

The render loop runs in the browser, not the server:

```js
let lastTick = 0;
const FRAME_MS = 1000 / 30;  // cap at device's 30 FPS

function tick(now) {
  if (now - lastTick >= FRAME_MS) {
    pyCoordinator.tick();        // calls Python via PyScript
    pyWebCanvas.to_imagedata();  // PIL Image → ImageData
    ctx.putImageData(imgData, 0, 0);
    lastTick = now;
  }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
```

The cap at 30 FPS is intentional: the device is a Pi 4 doing 30 FPS. Running the preview at 60+ FPS would make the preview look *smoother* than the sign, which could mislead operators about what the device will actually show. Capping matches the device's cadence.

`requestAnimationFrame` is paused automatically when the tab is hidden, so the preview doesn't burn CPU when the user is on a different tab. This is free scaling.

### Decision 5: Polling `/api/live-messages` every 3 s, mirroring `templates/testing.html`

The browser stays in sync with the Flask `MessageManager` by **polling `/api/live-messages?limit=1&suppress=true` every 3 seconds** — the same cadence and shape as `templates/testing.html`'s `setInterval(fetchMessages, 3000)`. Each successful poll returns the most recent filtered message; when the polled body differs from the last body the coordinator was handed, the browser calls `coordinator.request_message(body)`. The first poll also seeds the initial state on page load. If the response is an empty list, the coordinator keeps its current state (no `request_message` call, no effect cycle). The body-dedupe is what keeps the preview from cycling the effect on every tick when nothing has changed.

**Components:**

- **Browser:** a `setInterval(pollLatestMessage, 3000)` loop in `static/preview.js`. `pollLatestMessage()` does `fetch('/api/live-messages?limit=1&suppress=true')`, reads the JSON, and compares the first message's `body` (or `null` if empty) to a `lastShownBody` variable held in module scope. If they differ, it calls `coordinator.request_message(body)` via the PyScript interop and updates `lastShownBody`. The first invocation runs immediately at startup (matching the testing page's `fetchMessages(); setInterval(fetchMessages, 3000);` pattern). When the tab is hidden, browsers throttle `setInterval` (typically to 1 Hz, often less); this is acceptable for v1 — operators will see updates immediately on tab focus. The poll loop does **not** open a WebSocket or persistent push channel.
- **Flask:** no changes. The existing `/api/live-messages` endpoint already exists and returns the filtered message list, newest-first. The existing `api_login_required` decorator gates it for authenticated session/API-key users (the preview page is `@login_required` already, so the JS will be authenticated via the session cookie).
- **No `PreviewBroadcaster`**, no WebSocket route, no `_on_message` wiring in the Flask process. All that is parked.

**Server cost per tab:**

- Per page load: 1 authenticated GET to `/api/live-messages?limit=1&suppress=true` (a small JSON payload, ~100 bytes).
- Per tab: 1 GET every 3 s while the tab is visible (same shape and cadence as the existing testing page, which already produces 20 GETs/min/tab with the same payload).
- Per the testing page precedent, the server's per-tab polling load is small enough that 5+ open preview tabs are not a problem on the same Flask process.

**Why this is right for v1:**

- **Mirrors a working pattern in the codebase.** `templates/testing.html` already runs a 3 s poll against `/api/live-messages`; preview.js just clones the same loop. The polling cost is not new server load — it's the same shape operators already accept on the testing page.
- **No new server-side code.** Reuses the existing `/api/live-messages` endpoint. The Flask process stays a static file host; nothing else changes.
- **No new dependencies.** No `Flask-Sock`, no WebSocket lifecycle management, no Heroku router caveats.
- **Reversible.** When the algorithm rework lands, the browser can change its `?limit=1` to `?limit=N` and add a rotation loop that calls `coordinator.request_message(body)` for each message in turn. The coordinator's `request_message` API is already the right shape; the caller is just changing from "poll + dedupe" to "poll + rotate."

**Why we don't ship a real-time channel now:**

The user has flagged that the message-rotation algorithm is changing. Two plausible shapes for the future:

1. **Server-push rotation:** A new MQTT topic or WebSocket pushes a sequence of messages to the browser, driven by whatever algorithm decides what to show next.
2. **Client-side rotation:** The browser fetches a set of messages (e.g. last 50) and runs a rotation algorithm locally — e.g. newest first, weighted random, or time-based.

Picking the wrong one now would burn implementation effort. The 3 s polling + dedupe is the smallest thing that ships; it works for v1 and leaves both options open. If we need real-time, the broadcaster / WebSocket can be added in a follow-up change.

**Future revision note (in Open Questions, not in scope):** When the message-rotation algorithm lands, a follow-up change will replace this Decision with whatever the new shape requires. The coordinator's `request_message(body)` API is stable across both possibilities.

### Decision 6: Canvas sizing is computed in the browser

The browser draws a 64×64 source frame, then computes the on-screen size: `min(800, availableWidth)` clamped to an integer multiple of 64 so pixels stay square and aligned. The `<canvas>` `width`/`height` attributes are set to that pixel size, and CSS scales it down with `image-rendering: pixelated` for the LED look.

### Decision 7: Effects that need external assets (PNG, video) gracefully degrade

`png_display` reads from `design/pngs/`, `video_display` from `design/videos/`. On the device, these are on the Pi's filesystem. In the browser, these assets need to be either:

- Served as static files (small PNGs, OK) and the pattern's `__init__` is monkey-patched to fetch them
- Or skipped entirely (the browser's filesystem sandbox can't read arbitrary paths)

`video_display` is the hard one: it uses OpenCV, which is not in Pyodide by default. Easiest path is to skip `video_display` in the browser preview. (Could revisit if needed, but it's an unusual effect.)

`png_display` could work if the PNGs are served as static files and the pattern is given their URLs instead of paths. The simplest v1 is to **skip both** in the browser and document it. The other four effects (fireworks, flame, nightsky, honeycomb) all work in the browser unchanged.

If a pattern's `__init__` raises in the browser (whether because of missing assets or any other reason), the preview logs a warning and excludes the effect from the cycle — same graceful-degrade behavior as the prior design.

## Why the preview is not a perfect mirror of the device

Even with the same Python effect code in the browser, the preview will diverge from the sign. Worth being explicit:

- **`time.monotonic()` differs.** The Pi's coordinator has been running for some time; the browser's coordinator starts fresh when the page loads.
- **RNG state differs.** `fireworks.py` and `flame.py` both pull from `random` (palette selection, spawn positions, particle velocities, per-cell cooling). The browser and the Pi have different RNG states from the first tick.
- **Persistent effect state diverges.** `flame._heat` is a 64×64 buffer; `fireworks.particles` is a list. They start at zero in the preview and are non-zero on the device the moment the device boots.
- **MQTT ordering / duplicates.** A given SMS may be received by the device first, by Flask first, or by neither (broker drop). The preview is one consumer of Flask's MessageManager; the device is the other.
- **Clock speed / cadence.** Browser's `requestAnimationFrame` vs Pi's `SwapOnVSync` vs `time.monotonic()`. Even if we cap both at 30 FPS, jitter differs.

So the preview is "what the sign *would* display given the same effect code, the same scroller, and the same filtered message stream." It is **not** "exactly what the sign is displaying right now." Operators should use it to verify *intent* (does my filter exclude the right messages? does my new scroller text look right?) not *state* (is the device rendering frame #1234 right now?).

If we ever need pixel-perfect mirroring, the device would need to publish its coordinator's `start_time`, `idx`, and `mode` to MQTT on a heartbeat, and the browser's coordinator would resume from that state. Big change for a niche use case; not in scope.

## Risks / Trade-offs

- **[Risk] PyScript cold start (5–10 s first load)** → Mitigation: spinner + "Loading preview…" message; lazy-init only when the user opens `/preview`; browser cache makes subsequent loads 1–2 s.
- **[Risk] PyScript bundle size (~10 MB)** → Mitigation: HTTP cache headers set to `max-age=31536000, immutable`; documented in the page; not a problem for an operator-facing preview. If it becomes a problem, drop to raw Pyodide and trim what's loaded.
- **[Risk] `video_display` and `png_display` may not work in the browser without extra plumbing** → Mitigation: skip them in v1; the other four effects (fireworks, flame, nightsky, honeycomb) cover the most-used cases. Add them in a follow-up if needed.
- **[Risk] PyScript and Flask CSP** → Mitigation: configure CSP to allow `wasm-unsafe-eval` and `script-src` for the PyScript CDN; document the required headers.
- **[Risk] Browser console errors from missing `rgbmatrix`** → Mitigation: don't load `scroller.py` (the rgbmatrix-using `MatrixScroller`) in the browser. The PyScript entry point imports only `lib_shared/scroller_base.py` and `heart-message-manager/preview_scroller.py` (the Pillow subclass).
- **[Trade-off] Preview shows the latest message via 3 s polling (not push)** → Accepted for v1. Mirrors the cadence the existing `/testing` page already uses, so the per-tab server load is not new. The future rotation-algorithm change will replace this with whatever live-channel shape that work needs (push, rotation over a fetched set, etc.).
- **[Trade-off] Preview and device diverge in RNG and timing** → Accepted. Documented above. The preview is an *intent* tool, not a *state* tool.
- **[Trade-off] Browser-driven timing means the preview never *exactly* matches the device** → Same as above; same mitigation (document, don't try to solve).

## Migration Plan

No data migration. Deployment steps:

1. Add `lib_shared/scroller_base.py` (new file with the shared scroller logic) and `heart-message-manager/preview_scroller.py` (new file with the Pillow subclass). Rename the existing `Scroller` class in `heart-matrix-controller/scroller.py` to `MatrixScroller` and have it inherit from `ScrollerBase`. Update `heart-matrix-controller/main.py` to use `MatrixScroller`.
2. Add the PyScript config (`py-config.toml` declaring Pillow + numpy) and a `<py-script>` block on `/preview`
3. Ship the effect modules + `WebDisplay` / `WebCanvas` + the scroller base + `PreviewScroller` as static files (or import them via `<py-script src="…">` URLs)
4. Add `static/preview.js` to bootstrap PyScript, fetch `/api/live-messages?limit=1&suppress=true` on load, hand the latest body to the coordinator, drive the main loop, and update the status block
5. Verify locally: open `/preview`, confirm PyScript loads, canvas animates ≥ 30 FPS, the most recent filtered message is scrolled on load
6. Deploy — no settings.toml changes required; effects that fail to init are skipped

Rollback: remove the new template + JS + PyScript config, revert the `Scroller` → `MatrixScroller` rename. No DB / S3 / MQTT state to roll back.

## Open Questions

- When the future Settings/Playlists system lets the operator change the effect list, the preview should reflect that immediately. We don't need to solve it now (no Settings/Playlists UI exists yet), but the browser-side `PreviewCoordinator`'s effect list should be constructable from a config snapshot fetched from the server, rather than a hardcoded import. Easy to add when needed.
- **When the message-rotation algorithm lands, the preview needs a way to receive a stream or rotation set of messages instead of polling the single latest.** The two plausible shapes are (a) server-push via MQTT/WebSocket and (b) client-side rotation over a fetched set. A follow-up change will pick one and replace Decision 5. The coordinator's `request_message(body)` API is stable across both.
- **Should we re-add `video_display` and `png_display` in the browser?** Defer to v2. They'd need asset URL discovery (which PNGs to load?) and an OpenCV-equivalent in WASM. Not worth the complexity for v1.
