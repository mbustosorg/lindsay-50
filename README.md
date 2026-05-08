# Lindsay's 50th Heart Sign

Send a text message to trigger actions on an ESP32 over the internet.

```
SMS → Twilio → Flask server → MQTT broker → ESP32 (CircuitPython)
```

## How it works

1. You send an SMS to a Twilio phone number
2. Twilio forwards it to the Flask server via webhook
3. The server publishes the message body to an MQTT topic
4. The ESP32 receives it and runs your custom logic

## Requirements

- Python 3.11+
- A [Twilio](https://twilio.com) account with a phone number
- An MQTT broker (e.g. [HiveMQ Cloud](https://www.hivemq.com/cloud/) free tier)
- ESP32 with [CircuitPython](https://circuitpython.org) installed
- [Adafruit CircuitPython Bundle](https://circuitpython.org/libraries) (`adafruit_minimqtt`)

## Setup

### 1. Configuration

Edit `heart-matrix-controller/settings.toml` with your credentials — both the server and the ESP32 read from this file:

```toml
WIFI_SSID = "your-wifi"
WIFI_PASSWORD = "your-password"

MQTT_HOST = "your-broker-host"
MQTT_PORT = 8883
MQTT_TOPIC = "sms/incoming"
MQTT_USERNAME = "username"
MQTT_PASSWORD = "password"

# Optional: only allow messages from these numbers (comma-separated)
# ALLOWED_SENDERS = "+15551234567,+15559876543"
```

### 2. Flask server

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r heart-sms-receiver/requirements.txt
python heart-sms-receiver/main.py
```

### 3. Expose to Twilio

```bash
ngrok http 5000
```

Copy the `https://....ngrok-free.app` URL.

### 4. Configure Twilio

In the Twilio Console → your phone number → **Messaging**:
- **A message comes in**: Webhook, `POST`
- URL: `https://your-ngrok-url/sms`

### 5. ESP32

Copy these files to your `CIRCUITPY/` drive:
- `heart-matrix-controller/code.py` → `CIRCUITPY/code.py`
- `heart-matrix-controller/settings.toml` → `CIRCUITPY/settings.toml`

Copy `adafruit_minimqtt/` from the Adafruit CircuitPython Bundle to `CIRCUITPY/lib/`.

## Adding device logic

Edit the `on_message()` function in `heart-matrix-controller/code.py`:

```python
def on_message(client, topic, message):
    if message.strip().lower() == "on":
        led.value = True
    elif message.strip().lower() == "off":
        led.value = False
```

## Testing

```bash
curl -X POST http://localhost:5000/sms \
  -d "From=%2B15551234567&Body=hello+world&To=%2B15559999999"
```
