# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → display bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-message-manager/main.py`), which publishes the body to an Adafruit IO feed via MQTT. A Raspberry Pi 4 (`heart-matrix-controller/main.py`) subscribes to that feed over MQTT and renders the message as scrolling text on a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine wired) over a night-sky / fireworks / flame background that cycles on each new message.

The display device was originally an ESP32 running CircuitPython and was migrated to a Raspberry Pi 4: native `logging` replaces `adafruit_logging`, `paho-mqtt` replaces the CircuitPython `adafruit_io` MQTT client, and the rendering layer was ported from displayio (retained scene graph, auto-refresh) to the immediate-mode hzeller `rpi-rgb-led-matrix` API (`rgb_display.py` blits an offscreen canvas each frame and `SwapOnVSync`es it).

Flask also subscribes to the same MQTT feed to keep its live message ring buffer in sync with the display device.

## Project structure

```
lindsay-50/
├── heart-message-manager/        # Flask server (SMS receiver + admin UI)
│   ├── main.py                  # Flask app entrypoint
│   ├── sqlite.py               # SQLite storage (rebuild-from-S3 on startup)
│   ├── s3.py                   # S3 backup helpers
│   ├── server_time.py          # Time helpers (zoneinfo-based, avoids stdlib conflict)
│   ├── auth.py                 # User auth + API-key / Twilio webhook verification
│   ├── templates/              # Jinja2 templates
│   ├── settings.toml           # Local config (gitignored)
│   └── settings.toml.example
├── heart-matrix-controller/      # Raspberry Pi 4 display device
│   ├── main.py                 # Entrypoint: builds Display + patterns, runs the loop
│   ├── rgb_display.py          # hzeller rgbmatrix wrapper + Bitmap/Palette/Effect
│   ├── scroller.py             # Scrolling text via rgbmatrix graphics + BDF font
│   ├── patterns/               # Background patterns (Effect subclasses)
│   │   ├── fireworks.py
│   │   ├── flame.py
│   │   ├── nightsky.py
│   │   ├── png_display.py      # PNG slideshow from design/pngs (crossfade)
│   │   ├── video_display.py    # Looping video (OpenCV) from design/videos
│   │   ├── honeycomb.py        # Pixelblaze HSV pattern port (numpy + SetImage)
│   │   └── hyperspace.py       # Star Wars-style jump: 3D starfield → tunnel of streaks
│   └── settings.toml            # Local config (gitignored)
├── lib_shared/                  # Shared code (Flask + Pi device)
│   ├── models.py               # Message, SignConfig, FilterRule, RenderingSettings
│   ├── messages.py             # FilteredMessages, InMemoryMessages
│   ├── message_manager.py      # MessageManager (dispatch + seed)
│   ├── config_reader.py        # TOML + env config loader
│   ├── log_setup.py            # Shared logging format (Los Angeles timestamps)
│   ├── mqtt_factory.py         # Selects the adafruit/paho MQTT client
│   ├── adafruit_mqtt_client.py # Adafruit IO MQTT client (Heroku)
│   └── paho_mqtt_client.py     # Paho MQTT client (local dev + Pi)
├── design/
│   ├── pngs/                    # artwork for the png_display pattern
│   └── videos/                  # clips for the video_display pattern
├── scripts/                     # start/stop helpers, Pi systemd service + startup
├── requirements.txt
└── .venv/
```

## First-time setup

```bash
# Create venv and install dependencies
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

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

- `heart-message-manager/main.py` — Flask app, publishes envelopes via MQTT client, serves admin UI.
- `lib_shared/mqtt_factory.py` — `make_mqtt_client()` picks the client from `MQTT_CLIENT` (defaults to paho); both entrypoints call it.
- `lib_shared/adafruit_mqtt_client.py` — wraps `Adafruit_IO.MQTTClient` (Heroku, `MQTT_CLIENT="adafruit"`).
- `lib_shared/paho_mqtt_client.py` — wraps `paho-mqtt`; subscribe loop in a daemon thread (auto-reconnect), plus `publish_envelope()` for Flask. Used by local dev and the Pi.
- `heart-matrix-controller/rgb_display.py` — Pi: wraps hzeller `RGBMatrix`; provides `Bitmap`/`Palette`/`arrayblit` (the displayio subset the effects use), the `Effect` base, and the per-frame composite (`Display.render`).
- `heart-matrix-controller/main.py` — Pi entrypoint; seeds, starts MQTT, runs `EffectCoordinator.tick()` which advances + composites each frame.
- `lib_shared/message_manager.py` — Shared `MessageManager`; Flask seeds from REST API, the Pi seeds from Flask's REST API.

## Browser runtime: PyScript, not a separate JS app

The browser preview runs Python via [PyScript](https://pyscript.net/) (Pyodide 0.26 / PyScript 2024.9.x). This is a deliberate architectural choice, not an implementation detail:

- `lib_shared/` is shared across THREE runtimes: the Flask server, the Raspberry Pi device, and the browser. The browser reuses the same Python classes the server and Pi use — `MessageManager`, `FilteredMessages`, `EffectsCoordinator`, the patterns, the scroller, the message models. As much of the server-side code as possible runs in the browser unchanged.
- Browser-specific I/O is done via Pyodide's `js.X` proxy: `js.fetch` for HTTP, `js.indexedDB` for persistence, `js.window` for cross-realm references. Classes in `lib_shared/` access browser APIs through this proxy — but the classes themselves stay Python.
- `heart-message-manager/static/*.py` are PyScript wrappers around native JS shims (e.g. `MessageBufferStore.py` wraps `message_buffer_store.js`'s IDB shim; `MqttWsClient.py` wraps `mqtt_ws_client.js`'s MQTT-WS shim). They give Python code a clean interface to browser-only APIs. **They are not ports of `lib_shared/` classes** — if a `*.py` under `static/` shadows a `lib_shared/` class with the same name, that's a bug.
- Adding a new storage backend for the message service means adding a new Python class to `lib_shared/messages.py` (or wherever the storage lives) that subclasses `FilteredMessages` and uses `js.X` for browser I/O if needed. It does NOT mean creating a JS implementation. The current pair is `InMemoryMessages` (server, Pi) and `IndexedDBMessages` (browser, uses `js.indexedDB`) — both Python, both in `lib_shared/`.
- The `is_browser` flag in `MessageManager` already drives the seed-fetch runtime (`js.fetch` vs `requests`); the same flag (or a `storage=` kwarg) drives the storage backend pick at construction time.

If a design conversation drifts toward "let's write a JS version of `MessageManager`" or "let's add a JS class in `static/` that does what `lib_shared/X.py` does", that is a sign the design has lost the plot. Reset, and reuse the Python.

## Raspberry Pi 4 setup

Wi-Fi is managed by the Pi OS (`nmcli` / `raspi-config`), not this process. The LED panel is driven by the [hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library (its Python bindings, `rgbmatrix`, are pulled in by `requirements.txt`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r heart-matrix-controller/requirements.txt   # builds the rgbmatrix C extension

# Scrolling text uses `heart-matrix-controller/fonts/8x13.bdf`, which
# ships with the repo (the hzeller rpi-rgb-led-matrix 8x13 font, public
# domain). Override via the FONT_PATH key in settings.toml if you want
# to use a different font.

cp heart-matrix-controller/settings.toml.example heart-matrix-controller/settings.toml
# fill in MQTT_*, the API URLs, FONT_PATH, and the MATRIX_* panel geometry
```

Run from the `heart-matrix-controller/` directory so `settings.toml` and the relative `FONT_PATH` resolve, with the repo root on `PYTHONPATH` for `lib_shared`. The hzeller library needs root for GPIO:

```bash
cd heart-matrix-controller
sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 main.py
```

### Run as a systemd service

`scripts/lindsay_50.service` runs the controller at boot via `scripts/startup_matrix_server.sh` (which cds into `heart-matrix-controller/`, activates the repo-root `.venv`, sets `PYTHONPATH` to the repo root, and runs `main.py` as root). Both files assume the repo is cloned at `/home/pi/projects/lindsay-50` — edit `REPO_DIR` in the script and the paths in the unit file if yours differs.

```bash
sudo cp scripts/lindsay_50.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lindsay_50
journalctl -u lindsay_50 -f        # follow logs
```

Panel geometry (rows/cols/chain/mapper/hardware mapping/pwm bits/gpio slowdown) is configured via the `MATRIX_*` keys in `settings.toml` — see `settings.toml.example`. The defaults assume a 64×64 logical panel built from two 64×32 panels, serpentine-wired (chain of 2 folded by the `U-mapper`), wired directly to GPIO (`MATRIX_HARDWARE_MAPPING = "regular"`; use `"adafruit-hat"` for the Adafruit HAT/Bonnet). Verify `MATRIX_HARDWARE_MAPPING` and `MATRIX_PIXEL_MAPPER` against your actual wiring.

The scroller adapts to panel height: a 64×64 stack shows two scrolling lines (one centered per 64×32 half); a single short panel (`display.height <= 32`) shows one line centered on the whole display. For a single 32×64 test panel, set `MATRIX_CHAIN = 1` and `MATRIX_PIXEL_MAPPER = ""`.

To add a new visual pattern, drop a module in `heart-matrix-controller/patterns/` that subclasses `Effect` (from `rgb_display.py`) and append an instance to the list passed to `EffectCoordinator` in `main.py`. Two flavors:

- **Palette-based** (e.g. `fireworks`, `flame`, `nightsky`, `hyperspace`): set `self.bitmap` (a `Bitmap`), `self.palette` (a `Palette`), and optionally `self.scale`, call `self._init_render()` once the palette is populated, and implement `tick()` to update the bitmap. `Effect` supplies `set_brightness(b)` (fades by scaling the palette) and the default `render(canvas)`. Note: `self.scale` is reserved — `Effect.render()` reads it as an integer pixel-doubling factor (each lit pixel becomes a `scale × scale` block, default 1), so don't reuse the name for an unrelated "scale" of your own (give it a distinct name like `proj_scale`).
- **Full-color** (e.g. `video_display`, `honeycomb`): override `render(canvas)` to blit a whole RGB frame with `canvas.SetImage(pil_image)` — far faster than per-pixel `SetPixel` and not limited to 256 colors. Override `set_brightness(b)` to store a factor and apply it when blitting (the palette pipeline is bypassed). `png_display` is a hybrid: palette-based but overrides `render` to draw every pixel.
