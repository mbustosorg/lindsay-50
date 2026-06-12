# Lindsay's 50th Heart Sign

SMS → Twilio webhook → Flask server → MQTT broker → Raspberry Pi 4

Send a text message to a Twilio phone number. A Raspberry Pi 4 displays it on a 64×64 LED matrix.

## Architecture

```
SMS → Twilio → POST /api/messages → Flask
                                      │
                                      ├─→ SQLite (persistent storage)
                                      ├─→ S3 (source of truth backup)
                                      │
                                      └─→ MQTT broker ──→ Raspberry Pi 4 subscribes
                                                   ↑
                                          Flask also subscribes (ring buffer)
```

The Raspberry Pi 4 subscribes to a feed on the MQTT broker and renders incoming
messages on a 64×64 HUB75 LED panel (two stacked 64×32 panels) using the hzeller
`rpi-rgb-led-matrix` library. Flask also subscribes to the same feed to populate
its live message ring buffer.

## Setup

### 1. Install dependencies

```bash
git clone https://github.com/mbustosorg/lindsay-50
cd lindsay-50
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

(`./scripts/setup-dev-tools.sh` is optional — it installs OpenSpec,
agent-orchestrator, and the GitHub CLI, not the app dependencies.)

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

Start Flask with local MinIO (S3) + Mosquitto (MQTT) containers (started by default):

```bash
./scripts/start-app.sh
```

Or Flask only, against real S3/AIO:

```bash
./scripts/start-app.sh --flask-only
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

## Raspberry Pi 4 (display device) Setup

The display runs on a Raspberry Pi 4 driving a 64×64 HUB75 panel via the hzeller
[rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library. Wi-Fi
is managed by the Pi OS, not this process.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r heart-matrix-controller/requirements.txt   # builds the rgbmatrix C extension

cp heart-matrix-controller/settings.toml.example heart-matrix-controller/settings.toml
# fill in MQTT_*, the API URLs, FONT_PATH, and the MATRIX_* panel geometry

# Run from the controller dir (so settings.toml/FONT_PATH resolve); root is needed for GPIO:
cd heart-matrix-controller
sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 main.py
```

To run it at boot, install the systemd unit `scripts/lindsay_50.service`. See
**Raspberry Pi 4 setup** in [CLAUDE.md](CLAUDE.md) for the `MATRIX_*` panel
geometry keys and the service install steps.

### Patterns

The display cycles through background patterns (one switches in with each new
message), with the scrolling text composited on top:

| Pattern | Notes |
|---------|-------|
| `flame`, `fireworks`, `nightsky` | Generative palette-based effects |
| `png_display` | Slideshow of PNGs in `design/pngs/` (crossfades; `PNG_INTERVAL`, `PNG_FADE`) |
| `video_display` | Loops a video from `design/videos/` — or `VIDEO_PATH` (`VIDEO_FPS` to override). Needs OpenCV; pre-scale clips to 64×64 with ffmpeg |
| `honeycomb` | Port of a Pixelblaze HSV pattern (numpy) |
| `hyperspace` | Star Wars-style jump: a 3D starfield that stretches into a tunnel of streaks and back |

## Project structure

```
lindsay-50/
├── heart-message-manager/        # Flask server (SMS receiver + admin UI)
│   ├── main.py                  # Flask app entrypoint
│   ├── auth.py                  # User auth + API-key / Twilio webhook verification
│   ├── sqlite.py                # SQLite storage (rebuild-from-S3 on startup)
│   ├── s3.py                    # S3 backup helpers
│   ├── server_time.py           # Time helpers (zoneinfo-based)
│   ├── templates/               # Jinja2 templates
│   └── settings.toml.example
├── heart-matrix-controller/      # Raspberry Pi 4 display device
│   ├── main.py                  # Entrypoint: builds Display + patterns, runs the loop
│   ├── rgb_display.py           # hzeller rgbmatrix wrapper + Bitmap/Palette/Effect
│   ├── scroller.py              # Scrolling text via rgbmatrix graphics + BDF font
│   ├── patterns/                # Background patterns (Effect subclasses)
│   │   ├── fireworks.py
│   │   ├── flame.py
│   │   ├── nightsky.py
│   │   ├── png_display.py       # PNG slideshow from design/pngs (crossfade)
│   │   ├── video_display.py     # Looping video (OpenCV) from design/videos
│   │   ├── honeycomb.py         # Pixelblaze HSV pattern port (numpy + SetImage)
│   │   └── hyperspace.py        # Star Wars-style jump: 3D starfield → tunnel of streaks
│   └── settings.toml.example
├── lib_shared/                   # Shared code (Flask + Pi device)
│   ├── models.py                # Message, SignConfig, FilterRule, RenderingSettings
│   ├── messages.py              # FilteredMessages, InMemoryMessages
│   ├── message_manager.py       # MessageManager (dispatch + seed)
│   ├── config_reader.py         # TOML + env config loader
│   ├── log_setup.py             # Shared logging format (Los Angeles timestamps)
│   ├── mqtt_factory.py          # Selects the adafruit/paho MQTT client
│   ├── adafruit_mqtt_client.py  # Adafruit IO MQTT client (Heroku)
│   └── paho_mqtt_client.py      # Paho MQTT client (local dev + Pi)
├── scripts/                      # start/stop helpers, Pi systemd service + startup
├── requirements.txt
└── .venv/
```