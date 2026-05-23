# Lindsay's 50th Heart Sign

SMS → Twilio webhook → Flask server → MQTT broker → ESP32 (CircuitPython)

Send a text message to a Twilio phone number. The ESP32 displays it on the LED matrix.

## Architecture

```
SMS → Twilio → POST /api/messages → Flask
                                      │
                                      ├─→ SQLite (persistent storage)
                                      ├─→ S3 (source of truth backup)
                                      │
                                      └─→ MQTT broker ──→ ESP32 subscribes
                                                   ↑
                                          Flask also subscribes (ring buffer)
```

ESP32 subscribes to a feed on the MQTT broker and renders incoming messages on a 64×64 HUB75 LED panel. Flask also subscribes to the same feed to populate its live message ring buffer.

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/mbustosorg/lindsay-50
cd lindsay-50
./scripts/setup-dev-tools.sh
```

### 2. Configure

```bash
cp heart-message-manager/settings.toml.example heart-message-manager/settings.toml
# Edit settings.toml with your credentials
```

Required credentials (env vars override `settings.toml`):

```toml
# MQTT / Adafruit IO
MQTT_CLIENT = "adafruit"           # "adafruit" (Heroku) or "paho" (local dev)
MQTT_HOST = "io.adafruit.com"
MQTT_PORT = 8883
MQTT_USERNAME = "your-aio-username"
MQTT_PASSWORD = "your-aio-key"    # same as AIO_KEY
MQTT_TOPIC = "your-feed-name"      # bare feed name or "user/feeds/feed" path

# AWS S3 (for message logging — use MinIO locally)
AWS_ACCESS_KEY_ID = "..."
AWS_SECRET_ACCESS_KEY = "..."
AWS_S3_BUCKET = "your-bucket"
AWS_S3_REGION = "us-east-1"
AWS_S3_ENDPOINT_URL = ""           # leave empty for real AWS, set for MinIO
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

Flask runs at **http://localhost:5000**

Stop:

```bash
./scripts/stop-app.sh --with-services
```

### 4. Expose to Twilio (for local dev)

```bash
ngrok http 5000
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
| Settings | `/settings` | Allowed senders, rendering defaults, sign name, filter rules |
| Preview | `/preview` | Shows filtered display output |
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

## Project structure

```
lindsay-50/
├── heart-message-manager/     # Flask server (SMS receiver + admin UI)
│   ├── main.py               # Flask app entrypoint
│   ├── sqlite.py            # SQLite storage
│   ├── s3.py                # S3 backup helpers
│   ├── server_time.py        # Time helpers (zoneinfo-based, not stdlib)
│   ├── adafruit_mqtt_client.py  # Adafruit IO MQTT subscriber (Heroku)
│   ├── paho_mqtt_client.py      # Paho MQTT subscriber (local dev)
│   ├── templates/            # Jinja2 templates
│   └── settings.toml.example
├── heart-matrix-controller/   # CircuitPython device code
│   ├── code.py
│   ├── mqtt_client.py       # CircuitPython MQTT client (adafruit_io)
│   ├── scroller.py
│   ├── fireworks.py
│   ├── flame.py
│   └── settings.toml.example
├── lib_shared/               # Shared code (Flask + CircuitPython)
│   ├── models.py            # Message, SignConfig, FilterRule, etc.
│   ├── messages.py          # InMemoryMessages ring buffer
│   ├── message_manager.py   # Dispatch + seed orchestration
│   └── config_reader.py     # TOML + env config loader
├── requirements.txt
└── .venv/
```
