# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → microcontroller bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-sms-receiver/main.py`), which publishes the body to an Adafruit IO feed via the REST API. An ESP32 running CircuitPython (`heart-matrix-controller/code.py`) subscribes to that feed over MQTT and renders the message as scrolling text on a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine wired) over a fireworks- or flame-effect background that toggles on each new message.

## First-time setup

Each subdirectory has a committed `settings.toml.example`; copy it to `settings.toml` and fill in real values. The bare filename `settings.toml` is in `.gitignore` (matches at any depth) so secrets stay local.

```bash
cp heart-sms-receiver/settings.toml.example heart-sms-receiver/settings.toml
cp heart-matrix-controller/settings.toml.example heart-matrix-controller/settings.toml
```

Server-side dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r heart-sms-receiver/requirements.txt
```

Device-side dependencies and copy targets: see "ESP32 / CircuitPython setup" below.

## Running the server

```bash
source .venv/bin/activate
python heart-sms-receiver/main.py
```

Runs on `http://0.0.0.0:5000`. Twilio webhook URL: `POST /sms`.

## Testing the webhook locally

```bash
curl -X POST http://localhost:5000/sms \
  -d "From=%2B15551234567&Body=hello+world&To=%2B15559999999"
```

## Configuration

The two `settings.toml` files use different keys because the server and device use different APIs:

`heart-sms-receiver/settings.toml` — Adafruit IO REST publish:
- `AIO_USERNAME`, `AIO_KEY`, `AIO_FEED`
- `ALLOWED_SENDERS` (optional comma-separated phone-number allow-list; empty = accept all)

`heart-matrix-controller/settings.toml` — Wi-Fi + Adafruit IO MQTT subscribe + log level:
- `WIFI_SSID`, `WIFI_PASSWORD`
- `MQTT_HOST` (`io.adafruit.com`), `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USERNAME`, `MQTT_PASSWORD`
- `LOG_LEVEL` (DEBUG / INFO / WARNING / ERROR / CRITICAL)

The same Adafruit IO key serves as both the REST API key (server side) and the MQTT password (device side). `MQTT_TOPIC` accepts either a bare feed name or a full `user/feeds/feed` path; `code.py` rsplits to recover the feed for `IO_MQTT.subscribe()`. TLS auto-enables when `MQTT_PORT == 8883`.

## Architecture

```
SMS → Twilio → POST /sms (main.py) → Adafruit IO REST → AIO feed
                                                           ↓ MQTT
                                                      ESP32 code.py
                                                           ↓
                                          EffectCoordinator.request_message()
                                                           ↓
                              fade out → toggle effect → set scroll text → fade in
```

- `heart-sms-receiver/main.py` — Flask app, single `/sms` route, publishes via `Adafruit_IO.Client.send_data`.
- `heart-matrix-controller/code.py` — CircuitPython entrypoint; runs `io.loop()` and `EffectCoordinator.tick()` in a tight loop.
- `heart-matrix-controller/scroller.py` — `Scroller`: two `Label`s scrolling right-to-left, top panel and bottom panel offset by 1s, time-based pixel advance.
- `heart-matrix-controller/fireworks.py` — `Fireworks`: rocket → apex-explode → spark physics on a 64×64 bitmap.
- `heart-matrix-controller/flame.py` — `Flame`: heat-field on a 32×32 internal grid drawn into a `Group(scale=2)` for cheap upscale; palette is capped to 40% brightness so total LED current stays within USB budget.

## ESP32 / CircuitPython setup

Copy the following from the Adafruit CircuitPython Bundle (matching your CircuitPython major version) into `CIRCUITPY/lib/`:

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
