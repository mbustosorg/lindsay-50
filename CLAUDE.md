# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → display bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-message-manager/main.py`), which publishes the body to an Adafruit IO feed via MQTT. A Raspberry Pi 4 (`heart-matrix-controller/code.py`) subscribes to that feed over MQTT and renders the message as scrolling text on a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine wired) over a night-sky / fireworks / flame background that cycles on each new message.

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
│   ├── adafruit_mqtt_client.py # Adafruit IO MQTT subscriber (Heroku)
│   ├── paho_mqtt_client.py     # Paho MQTT subscriber (local dev)
│   ├── templates/              # Jinja2 templates
│   ├── settings.toml           # Local config (gitignored)
│   └── settings.toml.example
├── heart-matrix-controller/      # Raspberry Pi 4 display device
│   ├── code.py                 # Entrypoint: builds Display + effects, runs the loop
│   ├── rgb_display.py          # hzeller rgbmatrix wrapper + Bitmap/Palette/Effect
│   ├── paho_mqtt_client.py     # Paho MQTT subscriber (daemon thread, auto-reconnect)
│   ├── scroller.py             # Scrolling text via rgbmatrix graphics + BDF font
│   ├── fireworks.py
│   ├── flame.py
│   ├── nightsky.py
│   └── settings.toml            # Local config (gitignored)
├── lib_shared/                  # Shared code (Flask + Pi device)
│   ├── models.py               # Message, SignConfig, FilterRule, RenderingSettings
│   ├── messages.py             # FilteredMessages, InMemoryMessages
│   ├── message_manager.py      # MessageManager (dispatch + seed)
│   └── config_reader.py        # TOML + env config loader
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
- `heart-message-manager/adafruit_mqtt_client.py` — Heroku: wraps `Adafruit_IO.MQTTClient`.
- `heart-message-manager/paho_mqtt_client.py` — Local dev: wraps `paho-mqtt`.
- `heart-matrix-controller/paho_mqtt_client.py` — Pi: subscribe-only `paho-mqtt` client in a daemon thread (auto-reconnect).
- `heart-matrix-controller/rgb_display.py` — Pi: wraps hzeller `RGBMatrix`; provides `Bitmap`/`Palette`/`arrayblit` (the displayio subset the effects use), the `Effect` base, and the per-frame composite (`Display.render`).
- `heart-matrix-controller/code.py` — Pi entrypoint; seeds, starts MQTT, runs `EffectCoordinator.tick()` which advances + composites each frame.
- `lib_shared/message_manager.py` — Shared `MessageManager`; Flask seeds from REST API, the Pi seeds from Flask's REST API.

## Raspberry Pi 4 setup

Wi-Fi is managed by the Pi OS (`nmcli` / `raspi-config`), not this process. The LED panel is driven by the [hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library (its Python bindings, `rgbmatrix`, are pulled in by `requirements.txt`).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r heart-matrix-controller/requirements.txt   # builds the rgbmatrix C extension

# Scrolling text needs a BDF font. Copy one from the rpi-rgb-led-matrix repo:
mkdir -p heart-matrix-controller/fonts
# cp <rpi-rgb-led-matrix>/fonts/6x9.bdf heart-matrix-controller/fonts/

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

To add a new visual effect, subclass `Effect` (from `rgb_display.py`): set `self.bitmap` (a `Bitmap`), `self.palette` (a `Palette`), and optionally `self.scale`, call `self._init_render()` once the palette is populated, and implement `tick()` to update the bitmap. `Effect` supplies `set_brightness(b)` and `render(canvas)`. Append an instance to the `effects` list passed to `EffectCoordinator` in `main.py`.
