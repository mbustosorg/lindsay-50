# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → microcontroller bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-message-manager/main.py`), which publishes the body to an Adafruit IO feed via MQTT. An ESP32 running CircuitPython (`heart-matrix-controller/code.py`) subscribes to that feed over MQTT and renders the message as scrolling text on a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine wired) over a fireworks- or flame-effect background that toggles on each new message.

Flask also subscribes to the same MQTT feed to keep its live message ring buffer in sync with the ESP32.

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
├── heart-matrix-controller/      # CircuitPython device code
│   ├── code.py
│   ├── mqtt_client.py          # CircuitPython MQTT client (adafruit_io)
│   ├── scroller.py
│   ├── fireworks.py
│   ├── flame.py
│   └── settings.toml            # Local config (gitignored)
├── lib_shared/                  # Shared code (Flask + CircuitPython)
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
                        ESP32 subscribes                        Flask subscribes
                        (display updates)                      (live ring buffer)
```

- `heart-message-manager/main.py` — Flask app, publishes envelopes via MQTT client, serves admin UI.
- `heart-message-manager/adafruit_mqtt_client.py` — Heroku: wraps `Adafruit_IO.MQTTClient`.
- `heart-message-manager/paho_mqtt_client.py` — Local dev: wraps `paho-mqtt`.
- `heart-matrix-controller/mqtt_client.py` — CircuitPython MQTT client wrapping `adafruit_io.IO_MQTT`.
- `heart-matrix-controller/code.py` — CircuitPython entrypoint; runs MQTT loop and `EffectCoordinator.tick()`.
- `lib_shared/message_manager.py` — Shared `MessageManager`; Flask seeds from REST API, ESP32 seeds from Flask REST API.

## ESP32 / CircuitPython setup

Download the Adafruit CircuitPython Bundle matching your CircuitPython version, then copy files to `CIRCUITPY/lib/`:

Single files:
- `adafruit_logging.mpy`
- `adafruit_connection_manager.mpy`
- `adafruit_ticks.mpy` (required by `adafruit_minimqtt`)
- `adafruit_requests.mpy` (required by `adafruit_io` / `adafruit_matrixportal`)

Folders:
- `adafruit_minimqtt/`
- `adafruit_io/`
- `adafruit_matrixportal/`
- `adafruit_portalbase/` (parent of `adafruit_matrixportal`, not imported directly)
- `adafruit_display_text/` (used by `scroller.py`)

Built into CircuitPython firmware (do not copy): `os`, `time`, `wifi`, `socketpool`, `displayio`, `terminalio`, `rgbmatrix`, `framebufferio`, `bitmaptools`.

Skip unless needed: `adafruit_esp32spi/` (only for non-S3 boards with an external WiFi co-processor); `adafruit_bitmap_font/` (only if switching from `terminalio.FONT` to a custom BDF/PCF font).

Files to copy onto `CIRCUITPY/`: `code.py`, `scroller.py`, `fireworks.py`, `flame.py`, `settings.toml`.

To add a new visual effect, implement a class with the same surface as `Fireworks`/`Flame` (a `tilegrid` attribute that's hideable, a `tick()` method, and a `set_brightness(b)` method) and append it to the `effects` list passed to `EffectCoordinator` in `code.py`.
