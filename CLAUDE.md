# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → display bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-message-manager/main.py`), which publishes the body to an Adafruit IO feed via MQTT. A Raspberry Pi 4 (`heart-matrix-controller/main.py`) subscribes to that feed over MQTT and renders the message as scrolling text on a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine wired) over a night-sky / fireworks / honeycomb background that cycles on each new message.

The display device was originally an ESP32 running CircuitPython and was migrated to a Raspberry Pi 4: native `logging` replaces `adafruit_logging`, `paho-mqtt` replaces the CircuitPython `adafruit_io` MQTT client, and the rendering layer was ported from displayio (retained scene graph, auto-refresh) to the immediate-mode hzeller `rpi-rgb-led-matrix` API (`rgb_display.py` blits an offscreen canvas each frame and `SwapOnVSync`es it).

Flask also subscribes to the same MQTT feed to keep its live message ring buffer in sync with the display device.

## Project structure

```
lindsay-50/
├── heart-message-manager/        # Flask server (SMS receiver + admin UI)
│   ├── main.py                  # Flask app entrypoint
│   ├── sqlite.py               # SQLite storage (rebuild-from-S3 on startup)
│   ├── s3.py                   # S3 backup helpers (incl. MMS media download)
│   ├── server_time.py          # Time helpers (zoneinfo-based, avoids stdlib conflict)
│   ├── auth.py                 # User auth + API-key / Twilio webhook verification
│   ├── templates/              # Jinja2 templates (incl. messages.html media column)
│   ├── settings.toml           # Local config (gitignored)
│   └── settings.toml.example
├── heart-matrix-controller/      # Raspberry Pi 4 display device
│   ├── main.py                 # Entrypoint: builds Display + patterns, runs the loop
│   ├── rgb_display.py          # hzeller rgbmatrix wrapper + Bitmap/Palette/Effect
│   ├── scroller.py             # Scrolling text via rgbmatrix graphics + BDF font
│   ├── patterns/               # Background patterns (Effect subclasses)
│   │   ├── fireworks.py
│   │   ├── nightsky.py
│   │   ├── image_display.py    # MMS image slideshow (PNG / JPEG / GIF / WebP)
│   │   ├── video_display.py    # MMS video clip loop (mp4 via OpenCV)
│   │   ├── media_cycler.py     # Per-message media cycler (image + video,
│   │   │                       #   picks randomly; falls back to rotation
│   │   │                       #   on exhausted)
│   │   ├── honeycomb.py        # Pixelblaze HSV pattern port (numpy + SetImage)
│   │   ├── hyperspace.py       # Star Wars-style jump: 3D starfield → tunnel of streaks
│   │   ├── browser_media_overlay.py  # preview-only: drives DOM <img>/<video>
│   │   └── command_handlers.py # issue #51: force-upgrade / restart / shutdown
│   │                           #   registered via MessageManager.register_handler
│   └── settings.toml            # Local config (gitignored)
├── lib_shared/                  # Shared code (Flask + Pi device + browser preview)
│   ├── models.py               # Message, SignConfig, FilterRule, RenderingSettings
│   ├── messages.py             # FilteredMessages, InMemoryMessages
│   ├── message_manager.py      # MessageManager (dispatch + seed)
│   ├── effects_coordinator.py  # media-override logic + Pi/browser dispatch
│   ├── effects_loader.py       # JSON registry of effects (fades, hold, fade-out)
│   ├── config_reader.py        # TOML + env config loader
│   ├── log_setup.py            # Shared logging format (Los Angeles timestamps)
│   ├── mqtt_factory.py         # Selects the adafruit/paho MQTT client
│   ├── adafruit_mqtt_client.py # Adafruit IO MQTT client (Heroku)
│   └── paho_mqtt_client.py     # Paho MQTT client (local dev + Pi)
├── design/
│   ├── pngs/                    # artwork for the image_display pattern
│   └── videos/                  # clips for the video_display pattern
├── scripts/                     # start/stop helpers, Pi systemd service + startup
├── requirements-flask.txt       # Flask server deps (Heroku + laptop dev)
├── requirements-pi.txt          # Pi display device deps (setup-pi.sh)
├── requirements-provisioner.txt # Laptop-side provisioner deps (provision-pi.sh)
└── .venv/
```

## First-time setup

```bash
# Create venv and install dependencies
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-flask.txt
# (Pi deps installed by scripts/setup-pi.sh; provisioner deps via
# requirements-provisioner.txt — see "Laptop provisioner" below.)

# Copy settings files and fill in values
cp heart-message-manager/settings.toml.example heart-message-manager/settings.toml
cp heart-matrix-controller/settings.toml.example heart-matrix-controller/settings.toml
```

## Running the server

```bash
source .venv/bin/activate
python heart-message-manager/main.py
```

Runs on `http://0.0.0.0:5000`. Twilio webhook URL: `POST /api/messages`.

## Testing the webhook locally

```bash
curl -X POST http://localhost:5000/api/messages \
  -d "From=%2B15551234567&Body=hello+world&To=%2B15559999999"
```

## Admin UI

Two UI variants available:

- **Original**: `http://localhost:5000/` (Bootstrap 5, functional)
- **Playful redesign**: `http://localhost:5000/playful` (Tailwind, Fredoka/Nunito fonts, indigo/pink gradient)

Both share the same functionality. The playful variant is served from `*-playful.html` templates at matching routes (`/playful`, `/playful/messages`, etc.).

## Configuration

The two `settings.toml` files use different keys because the server and device use different APIs:

`heart-message-manager/settings.toml` — MQTT broker settings:
- `MQTT_CLIENT` — `"adafruit"` (Heroku) or `"paho"` (local dev)
- `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TOPIC`

`heart-matrix-controller/settings.toml` — Wi-Fi + Adafruit IO MQTT subscribe + log level:
- `WIFI_SSID`, `WIFI_PASSWORD`
- `MQTT_HOST` (`io.adafruit.com`), `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USERNAME`, `MQTT_PASSWORD`
- `LOG_LEVEL` (DEBUG / INFO / WARNING / ERROR / CRITICAL)

Environment variables always take precedence over `settings.toml` values.

## MMS media pipeline (issue #38 / `add-image-and-video-support`)

When a Twilio webhook carries `NumMedia > 0`, the message is MMS — Twilio
includes time-limited signed URLs (`MediaUrl0..MediaUrlN`) we must copy to
our own S3 before downloading. The wire shape on the `Message` model now
includes `media: list[{type, url, ...}]` so the Pi's
`EffectsCoordinator` can pick the media-override path on the next fade-in.

**Flow.**

```
MMS → Twilio → POST /api/messages → Flask (download -> S3)
                                      │
                                      ├─→ SQLite media (JSON TEXT)
                                      ├─→ S3 media/images/<YYYY-MM>/<key>
                                      │     S3 media/videos/<YYYY-MM>/<key>
                                      │
                                      └─→ MQTT envelope {media: [...]} → Pi
                                                              │
                              ┌───────────────────────────────┴───────┐
                              ▼                                       ▼
                       MediaCycler (Pi)               BrowserMediaOverlay (preview)
                       PIL/cv2 + sign                 DOM <img>/<video> overlay
                       fs cache                       over canvas (no PIL)
```

The same envelope reaches both subscribers, so the preview doesn't need
a separate fetch. The Pi constructs a `MediaCycler` from the per-message
media list; the preview constructs a `BrowserMediaOverlay` (DOM-driven,
no PIL/cv2 needed). Coordinator picks one via the `is_browser=True`
constructor kwarg — mirrors `MessageManager(is_browser=True)`.

**Media proxy route (`GET /api/media/<key>`).** Flask serves a 302 → freshly-signed S3 URL
on each request, behind `api_login_required` auth. Both the Pi's
`requests.get(media.url)` and the browser's `<img src=/api/media/..>`
follow the same redirect. This means S3 credentials never leak into client
code, signed URLs are minted per-request (no permanent URL committed),
and CORS/auth stays server-side.

**Per-effects settings registry (`lib_shared/effects_loader.py`).** The
canonical effects list is now a JSON document loaded at startup
(`config/effects.json`); `EFFECTS_SETTINGS_OVERRIDE` env var points at a
replacement file. The `/settings` admin page iterates the loader's list
verbatim — never a hardcoded list in `main.py` or in
`models.py`. Operator-added effects show up the moment the override file
is in place; canonical removals (e.g. PNG slideshow → image slideshow)
propagate without code changes.

**Pi/browser dispatch.** `effects_coordinator.py:EffectsCoordinator`
takes `is_browser: bool = False` and a `media_api_base_url` kwarg. When
`is_browser=True`, the cycler helper returns a `BrowserMediaOverlay`
instead of `MediaCycler`. The browser path needs no PIL/cv2 imports — the
preview's `lib_shared/patterns/browser_media_overlay.py` exposes read-only
`current_media_url` / `current_media_kind` / `current_opacity` properties;
`preview.js` swaps the DOM element's `src` each frame.

**Coordination note (issue #38 §11.5).** Pre-caching on receive is OUT OF
SCOPE for this change — by design, no `/api/prefetch` endpoint, no MEMFS
warm-up. The Pi's `MediaCycler` lazily fetches each attachment on cycle
advance. The browser's `<img>` / `<video>` elements fetch on demand.

## Architecture

```
SMS → Twilio → POST /api/messages → Flask
                                      │
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
               SQLite              S3 (log)         MQTT broker
                                            (publish envelope)
                                                   │
                              ┌────────────────────┴────────────────────┐
                              ▼                                         ▼
                        Pi 4 subscribes                        Flask subscribes
                        (display updates)                      (live ring buffer)
```

```
Pi 4 → StatusWriter.tick() ─┬─→ .status.json (loader probe signal)
                            └─→ StatusPublisher.publish() → MQTT_STATUS_TOPIC
                                                              │
                                                              ▼
                                                     Flask subscribes
                                                              │
                                                              ▼
                                                    LatestSignStatus
                                                    (in-memory, RLock)
                                                              │
                                                              ▼
                                                GET /api/sign-status (one-shot
                                                  load-time fetch by browser)
                                                              │
                                                              ▼
                                                Browser WS subscription on
                                                MQTT_STATUS_TOPIC → live updates
                                                              │
                                                              ▼
                                                Dashboard pill + Settings page
```

- `heart-message-manager/main.py` — Flask app, publishes envelopes via MQTT client, serves admin UI. Subscribes to both `MQTT_TOPIC` (envelope flow) and `MQTT_STATUS_TOPIC` (status flow) via the dual-topic `PahoMqttClient` extension. Exposes `GET /api/sign-status` for browser load-time hydration.
- `lib_shared/mqtt_factory.py` — `make_mqtt_client()` picks the client from `MQTT_CLIENT` (defaults to paho); both entrypoints call it.
- `lib_shared/adafruit_mqtt_client.py` — wraps `Adafruit_IO.MQTTClient` (Heroku, `MQTT_CLIENT="adafruit"`).
- `lib_shared/paho_mqtt_client.py` — wraps `paho-mqtt`; subscribe loop in a daemon thread (auto-reconnect), plus `publish_envelope()` for Flask. Used by local dev and the Pi. Two-topic extension: optional `status_topic` + `status_dispatch_callback` for the status flow.
- `lib_shared/sign_status.py` — `LatestSignStatus` — Flask-side in-memory holder for the most recent `StatusSnapshot` (RLock-guarded, defensive-copy semantics, `received_at_wallclock()` ISO-8601 timestamp).
- `heart-matrix-controller/status.py` — `StatusSnapshot` (8-key shape: `schema_version`, `active_sha`, `short_sha`, `started_at`, `updated_at`, `uptime_seconds`, `mqtt_connected`, `last_error`) + `StatusWriter` (atomic file write + MQTT publish in the same `tick()` call, 5s cadence).
- `heart-matrix-controller/status_publisher.py` — `StatusPublisher` — long-lived paho publisher for the status flow (single client + `connect_async` + `loop_start`, non-blocking thread-safe `publish()` at QoS 0).
- `heart-matrix-controller/loader.py` — `BOOT_HOLD_S = 17.0` (3× status.json writes × 5s + 2s slack) — pre-swap probe hold. `_is_status_healthy` is the two-signal contract (`mqtt_connected === true` AND `last_error is None`); the legacy `last_tick_age_ms` check was removed when the field was dropped from the snapshot.
- `heart-matrix-controller/main.py` — Pi entrypoint; seeds, starts MQTT, runs `EffectCoordinator.tick()` which advances + composites each frame. Also instantiates `StatusPublisher` and passes it to `StatusWriter`, and constructs an `EventLog(path=cfg.if_exists("EVENT_LOG_PATH") or "data/events.jsonl", max_entries=int(cfg.if_exists("EVENT_LOG_MAX_ENTRIES") or 100))` — the source of truth for display-recency (issue #26).
- `heart-matrix-controller/event_log.py` — `EventLog` (Pi-side append-only JSONL log) + `IndexedDBEventLog` (browser-side mirror; per-browser, NOT synced). Both expose `append(event)` / `query(event_type, message_id, since)` / `last_for(message_id, event_type)`. Schema is exactly `{event_type, message_id, timestamp, received_at}` — `favorite` is read from the message record at pick time, not stored here. Bounded ring (default 100 entries, FIFO drop-oldest rewrite). See `docs/event-log.md`.
- `heart-matrix-controller/rgb_display.py` — Pi: wraps hzeller `RGBMatrix`; provides `Bitmap`/`Palette`/`arrayblit` (the displayio subset the effects use), the `Effect` base, and the per-frame composite (`Display.render`).
- `lib_shared/selector.py` — `MessageSelector` (issue #26). The Pi and the browser preview both call `pick(messages, now, event_log, current_event_type, favorites)` — identical class, identical algorithm. Reads `display_recency` from the event log, normalizes `send_recency` over the eligible set, applies an additive `W_DISPLAY * disp + W_SEND * send + W_FAVORITE * (1 if fav else 0)` score, and breaks ties on `(-score, received_at, message.id)`. Behavioral knobs (`W_DISPLAY`, `W_SEND`, `W_FAVORITE`, `SATURATION_SECONDS`, `OFFSET_SECONDS`, `USE_WEIGHTED_SELECTOR`) are module-level constants — NOT in `settings.toml`. Ships dark with `USE_WEIGHTED_SELECTOR=False`.
- `lib_shared/message_manager.py` — Shared `MessageManager`; Flask seeds from REST API, the Pi seeds from Flask's REST API.
- `heart-message-manager/static/sign_status.js` — Browser-side module: load-time `fetch('/api/sign-status')` (one-shot) + a second `createMqttWsClient` for the status topic + 5s `setInterval` re-render. Renders the Dashboard pill (4 states: live-healthy | live-degraded | unknown | offline) and the Settings-page Sign Health section. No-op on pages with neither `#sign-live-pill` nor `[data-sign-status-field]`.

## Pi upgrade controls (issue #51)

The Settings page exposes three operator-driven commands that publish
to the existing `MQTT_TOPIC` and one new endpoint for the resolver
the loader queries on boot.

- `GET /api/sign/settings` (Flask) — returns `{target_version, timezone}`.
  `target_version` is always a concrete 7-char short SHA: operator-pinned
  `cfg.sign.target_version` or, when empty, Flask's own running short
  SHA (HEROKU_SLUG_COMMIT preferred, git rev-parse fallback). The
  endpoint serializes via `_short_sha` so the wire form is deterministic.
- `POST /api/sign/commands/<action>` — publishes `type=command`
  envelopes (valid: `force-upgrade`, `restart`, `shutdown`). Returns
  202 on publish, 503 on broker failure, 404 on unknown action.
- `lib_shared/boot_config.py:SIGN_SETTINGS_PATH` — canonical endpoint
  constant imported by both the loader and Flask; renaming is caught
  at import time.
- `lib_shared/boot_config.py:fetch_sign_settings` — typed wrapper; returns
  the resolved 7-char short SHA, or None on any failure (network,
  non-200, malformed JSON, missing/empty `target_version`).
- `heart-matrix-controller/command_handlers.py` — three handlers
  registered via `MessageManager.register_handler` (action-str, zero-arg
  callable contract). `force_upgrade` uses `os.execvpe` with
  `LINDSAY50_FORCE_UPGRADE=1` to enter the loader's force-upgrade
  entrypoint which bypasses the AUTO_UPDATE gate.
- `heart-matrix-controller/loader.py:force_upgrade_main` — bypasses
  AUTO_UPDATE but uses the same SHA-check + stage + probe + swap logic
  as the regular `main()`. The dispatch happens at the top of
  `main()` via the `LINDSAY50_FORCE_UPGRADE` env var.
- Legacy `/api/sign/boot-config` is RETAINED for pre-issue-#51 Pis.
  The new loader code uses `/api/sign/settings` exclusively; the
  legacy `fetch_expected_sha` is a transitional safety net.
- AUTO_UPDATE stays in `settings.toml` (env override: `AUTO_UPDATE=false`).
  No UI checkbox in v1.
- `heart-message-manager/static/pi_upgrade_settings.js` — Settings-page
  JS shim: wires the three command buttons in `[data-upgrade-settings-field]`
  (Force upgrade, Restart, Shut down) and POSTs to
  `/api/sign/commands/<action>` with `X-API-Key` from
  `window.APP_CONFIG.auth.API_SECRET_KEY`. Each command is gated by a
  `confirm()` modal. No-op when the section is absent.
- `heart-message-manager/static/pi_apply_settings.js` — Settings-page
  JS shim for the Apply button + click-to-edit on the Target Pi
  version input. Reads `data-saved-value` (persisted
  `target_version`) and `data-flask-version-placeholder`
  (`placeholder` attribute) to drive dirty-state and focus-clear;
  Apply click submits the surrounding `<form method="POST">` via
  `form.requestSubmit(applyBtn)`. No-op when the section is absent.

The short-vs-short target comparison replaces the legacy full-SHA
match: `local = git rev-parse HEAD` (full 40 chars) versus
`target_version` (always 7 chars). `local_short = short_sha(local)` is
compared against `target`. The `_resolve_full_sha` helper then expands
the short back to full via `git rev-parse <sha>^{commit}` for the
`git worktree add` step.

## Browser runtime: PyScript, not a separate JS app

The browser preview runs Python via [PyScript](https://pyscript.net/) (Pyodide 0.26 / PyScript 2024.9.x). This is a deliberate architectural choice, not an implementation detail:

- `lib_shared/` is shared across THREE runtimes: the Flask server, the Raspberry Pi device, and the browser. The browser reuses the same Python classes the server and Pi use — `MessageManager`, `FilteredMessages`, `EffectsCoordinator`, the patterns, the scroller, the message models. As much of the server-side code as possible runs in the browser unchanged.
- Browser-specific I/O is done via Pyodide's `js.X` proxy: `js.fetch` for HTTP, `js.indexedDB` for persistence, `js.window` for cross-realm references. Classes in `lib_shared/` access browser APIs through this proxy — but the classes themselves stay Python.
- `heart-message-manager/static/*.py` are PyScript wrappers around native JS shims (e.g. `MessageBufferStore.py` wraps `message_buffer_store.js`'s IDB shim; `MqttWsClient.py` wraps `mqtt_ws_client.js`'s MQTT-WS shim). They give Python code a clean interface to browser-only APIs. **They are not ports of `lib_shared/` classes** — if a `*.py` under `static/` shadows a `lib_shared/` class with the same name, that's a bug.
- Adding a new storage backend for the message service means adding a new Python class to `lib_shared/messages.py` (or wherever the storage lives) that subclasses `FilteredMessages` and uses `js.X` for browser I/O if needed. It does NOT mean creating a JS implementation. The current pair is `InMemoryMessages` (server, Pi) and `IndexedDBMessages` (browser, uses `js.indexedDB`) — both Python, both in `lib_shared/`.
- The `is_browser` flag in `MessageManager` already drives the seed-fetch runtime (`js.fetch` vs `requests`); the same flag (or a `storage=` kwarg) drives the storage backend pick at construction time.

If a design conversation drifts toward "let's write a JS version of `MessageManager`" or "let's add a JS class in `static/` that does what `lib_shared/X.py` does", that is a sign the design has lost the plot. Reset, and reuse the Python.

## Raspberry Pi 4 setup

Wi-Fi is managed by the Pi OS (`nmcli` / `raspi-config`), not this process. The LED panel is driven by the [hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library (its Python bindings, `rgbmatrix`, are pulled in by `requirements-pi.txt`).

The Pi only needs its own deps (not the Flask server's). `scripts/setup-pi.sh`
runs `pip install -r requirements-pi.txt` automatically on first bootstrap.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-pi.txt   # builds the rgbmatrix C extension

# Scrolling text uses `heart-matrix-controller/fonts/8x13.bdf` by
# default. The repo also ships `heart-matrix-controller/fonts/6x9.bdf`
# as a smaller alternative — both are public-domain Markus Kuhn fonts
# vendored from the hzeller rpi-rgb-led-matrix repo. Switch via the
# FONT_PATH key in settings.toml.

cp heart-matrix-controller/settings.toml.example heart-matrix-controller/settings.toml
# fill in MQTT_*, the API URLs, FONT_PATH, and the MATRIX_* panel geometry
```

Run from the `heart-matrix-controller/` directory so `settings.toml` and the relative `FONT_PATH` resolve, with the repo root on `PYTHONPATH` for `lib_shared`. The hzeller library needs root for GPIO:

```bash
cd heart-matrix-controller
sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 main.py
```

### Run as a systemd service

`scripts/lindsay_50.service` runs the controller at boot via `scripts/startup_matrix_server.sh` (which cds into `heart-matrix-controller/`, activates the repo-root `.venv`, sets `PYTHONPATH` to the repo root, and runs `main.py` as root). Both files assume the repo is cloned at `/srv/lindsay-50` — edit `REPO_DIR` in the script and the paths in the unit file if yours differs.

```bash
sudo cp scripts/lindsay_50.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lindsay_50
journalctl -u lindsay_50 -f        # follow logs
```

Panel geometry (rows/cols/chain/mapper/hardware mapping/pwm bits/gpio slowdown) is configured via the `MATRIX_*` keys in `settings.toml` — see `settings.toml.example`. The defaults assume a 64×64 logical panel built from two 64×32 panels, serpentine-wired (chain of 2 folded by the `U-mapper`), wired directly to GPIO (`MATRIX_HARDWARE_MAPPING = "regular"`; use `"adafruit-hat"` for the Adafruit HAT/Bonnet). Verify `MATRIX_HARDWARE_MAPPING` and `MATRIX_PIXEL_MAPPER` against your actual wiring.

The scroller adapts to panel height: a 64×64 stack shows two scrolling lines (one centered per 64×32 half); a single short panel (`display.height <= 32`) shows one line centered on the whole display. For a single 32×64 test panel, set `MATRIX_CHAIN = 1` and `MATRIX_PIXEL_MAPPER = ""`.

To add a new visual pattern, drop a module in `heart-matrix-controller/patterns/` that subclasses `Effect` (from `rgb_display.py`) and append an instance to the list passed to `EffectCoordinator` in `main.py`. Two flavors:

- **Palette-based** (e.g. `fireworks`, `nightsky`, `hyperspace`): set `self.bitmap` (a `Bitmap`), `self.palette` (a `Palette`), and optionally `self.scale`, call `self._init_render()` once the palette is populated, and implement `tick()` to update the bitmap. `Effect` supplies `set_brightness(b)` (fades by scaling the palette) and the default `render(canvas)`. Note: `self.scale` is reserved — `Effect.render()` reads it as an integer pixel-doubling factor (each lit pixel becomes a `scale × scale` block, default 1), so don't reuse the name for an unrelated "scale" of your own (give it a distinct name like `proj_scale`).
- **Full-color** (e.g. `video_display`, `honeycomb`): override `render(canvas)` to blit a whole RGB frame with `canvas.SetImage(pil_image)` — far faster than per-pixel `SetPixel` and not limited to 256 colors. Override `set_brightness(b)` to store a factor and apply it when blitting (the palette pipeline is bypassed). `png_display` is a hybrid: palette-based but overrides `render` to draw every pixel.
