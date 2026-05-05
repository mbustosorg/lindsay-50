 # CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

SMS → microcontroller bridge. A Twilio webhook posts an incoming SMS to a Flask server (`heart-sms-receiver/main.py`), which publishes the message body to an MQTT topic. An ESP32 running CircuitPython (`heart-matrix-controller/code.py`) subscribes to that topic and acts on the message.

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

All credentials live in `heart-matrix-controller/settings.toml` — both the Flask server and the ESP32 read from this single file. The Flask server loads it at startup via `tomllib`. The ESP32 loads it via CircuitPython's `os.getenv()`.

Key settings: `MQTT_HOST`, `MQTT_PORT`, `MQTT_TOPIC`, `MQTT_USERNAME`, `MQTT_PASSWORD`. TLS is auto-enabled when `MQTT_PORT` is `8883`.

Optional `ALLOWED_SENDERS` (comma-separated phone numbers) restricts which numbers can trigger the device.

## Architecture

```
SMS → Twilio → POST /sms (main.py) → paho-mqtt publish → MQTT broker (HiveMQ Cloud)
                                                                    ↓
                                              ESP32 (code.py) subscribes → on_message()
```

- `heart-sms-receiver/main.py` — Flask app, single route `/sms`
- `heart-matrix-controller/code.py` — CircuitPython script; copy to `CIRCUITPY/code.py`
- `heart-matrix-controller/settings.toml` — shared config; copy to `CIRCUITPY/settings.toml`

## ESP32 / CircuitPython setup

Required library (copy to `CIRCUITPY/lib/`): `adafruit_minimqtt/` from the Adafruit CircuitPython Bundle.

Device logic goes in the `on_message()` function in `code.py`.
