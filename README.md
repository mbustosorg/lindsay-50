# Lindsay's 50th Heart Sign

SMS → Twilio webhook → Flask server → MQTT broker → ESP32 (CircuitPython)

Send a text message to a Twilio phone number. The ESP32 displays it on the LED matrix.

## Architecture

```
SMS → Twilio → POST /api/messages → Flask
                                      │
                                      ├─→ SQLite (persistent storage)
                                      ├─→ S3 (source of truth backup)
                                      └─→ MQTT broker ──→ ESP32 subscribes
```

ESP32 subscribes to `username/feeds/feedname` on the MQTT broker and renders incoming messages on a 64×64 HUB75 LED panel.

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/mbustosorg/lindsay-50
cd lindsay-50
./scripts/setup-dev-tools.sh
```

### 2. Configure

```bash
cp heart-sms-receiver/settings.toml.example heart-sms-receiver/settings.toml
# Edit settings.toml with your credentials
```

Required credentials:

```toml
# Adafruit IO (for MQTT broker)
AIO_USERNAME = "your-aio-username"
AIO_KEY = "your-aio-key"
AIO_FEED = "your-feed-name"

# AWS S3 (for message logging — use MinIO locally)
S3_BUCKET = "your-bucket"
S3_ENDPOINT_URL = ""  # leave empty for real AWS, set for MinIO

# MQTT (usually same as AIO credentials)
MQTT_HOST = "io.adafruit.com"
MQTT_PORT = 8883
```

### 3. Local development

Start Flask with local MinIO + Mosquitto:

```bash
./scripts/start-app.sh --with-services
```

Or without local services (uses real S3/AIO):

```bash
./scripts/start-app.sh
```

Flask runs at **http://localhost:5001**

Stop:

```bash
./scripts/stop-app.sh --with-services
```

### 4. Expose to Twilio (for local dev)

```bash
ngrok http 5001
```

In Twilio Console → your phone number → **Messaging**:
- **A message comes in**: Webhook, `POST`
- URL: `https://your-ngrok-url/api/messages`

## Admin UI

Flask serves an admin UI at:

| Page | Route | Purpose |
|------|-------|---------|
| Dashboard | `/` | Recent messages, counts |
| Messages | `/messages` | Paginated list with suppress/unsuppress |
| Filter Rules | `/filters` | Add/delete suppression rules |
| Settings | `/settings` | Allowed senders, rendering defaults, sign name |
| Testing | `/testing` | Inject test messages, live MQTT feed, config viewer |

## Message Filtering

Filter rules suppress messages by:

| Type | Matches |
|------|---------|
| `keyword` | Case-insensitive substring in body |
| `regex` | Python regex on body |
| `sender` | Exact E.164 phone number |
| `message` | Exact message UUID |

## Running Tests

```bash
source .venv/bin/activate
PYTHONPATH=. pytest tests/ -v
```

## ESP32 Setup

Copy to `CIRCUITPY/`:

- `heart-matrix-controller/code.py`
- `heart-matrix-controller/settings.toml`

Required CircuitPython libraries (from Adafruit Bundle):

- `adafruit_minimqtt/`
- `adafruit_io/`
- `adafruit_matrixportal/`
- `adafruit_connection_manager/`
- `adafruit_ticks.mpy`
- `adafruit_requests.mpy`
- `adafruit_logging.mpy`
