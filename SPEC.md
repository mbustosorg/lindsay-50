# Heart Matrix Sign — Project Specification

## Overview

An ESP32-based programmable LED matrix sign (64x64, 2-tile HUB75) that displays SMS messages sent by visitors. A Twilio webhook receiver (Flask on Heroku) processes incoming SMS, stores it to SQLite, logs to S3, and publishes to Adafruit IO via MQTT. The ESP32 subscribes to Adafruit IO MQTT for real-time messages, fetches config via HTTP, stores messages locally with UUID deduplication, filters them, and renders animated effects on the LED matrix.

Flask also subscribes to the same MQTT feed to maintain a live message ring buffer (MessageManager) for the admin UI live-messages endpoints.

---

## System Architecture

```
                        ┌──────────────────────────────────────────┐
                        │                    CLOUD                 │
                        │                                          │
   ┌────────┐           │  ┌────────────┐    ┌──────────┐          │
   │ Twilio │──────────▶│  │ Flask/     │────│   AWS    │          │
   │  SMS   │           │  │ Heroku     │    │    S3    │          │
   └────────┘           │  │            │────│ (logs)   │          │
                        │  └─────┬──────┘    └──────────┘          │
                        │        │                                 │
                        │        │ SQLite                          │
                        │        ▼                                 │
                        │  ┌────────────┐                          │
                        │  │  sqlite.py │                          │
                        │  └────────────┘                          │
                        │        │                                 │
                        │        │ publishes                       │
                        │        ▼                                 │
                        │  ┌────────────────────┐    ┌───────────┐ │
                        │  │    MQTT broker     │◀───│  ESP32    │ │
                        │  │ (Adafruit IO)      │    │  Matrix   │ │
                        │  └────────────────────┘    └───────────┘ │
                        │        ▲                                 │
                        │        │ MQTT (both subscribe)           │
                        └────────┼─────────────────────────────────┘
                                 │
                       ┌─────────┴─────────────────────────────────┐
                       │   ESP32 (CircuitPython)                   │
                       │   • MQTT subscribe (realtime).            │
                       │   • HTTP fetch history + config (seed).   │
                       │   • In-memory ring buffer (UUID dedup).   │
                       │   • Rebuilds from Flask REST API on boot. │
                       └───────────────────────────────────────────┘
```

### Communication Architecture

- **Flask**: S3 is the ultimate source of truth. On restart, Flask seeds SQLite from S3. For normal operation, Flask uses its local SQLite.
- **ESP32**: Flask REST API is the source of truth for historical messages and config. On boot, ESP32 fetches message history and config via HTTP. It subscribes to MQTT for real-time messages.
- **S3 message logging**: All inbound messages are logged to S3 as a durable backup. Flask rebuilds from S3 on restart.
- **MQTT as transport**: MQTT carries real-time messages to both ESP32 and Flask's MessageManager ring buffer.
- **Shared `lib_shared/`**: Both Flask and ESP32 share models, message management, and config loading via `lib_shared/`.

---

## Tech Stack

| Layer | Technology | Responsibility |
|-------|------------|----------------|
| SMS | Twilio | Receives SMS from visitors, sends webhook to Flask |
| Webhook Receiver | Flask (Python) on Heroku | Receives webhook, logs to S3, stores to SQLite, publishes to MQTT |
| Database | SQLite | Local message/config storage on Flask |
| Object Storage | AWS S3 | Durable message log, Flask rebuilds from S3 on restart |
| Message Bus | Adafruit IO MQTT | Real-time message transport to ESP32 and Flask |
| Sign Controller | ESP32 (CircuitPython) | Subscribes to MQTT, HTTP fetch for history/config, in-memory message store |
| Display | 64x64 HUB75 matrix (2× 64x32 tiles) | Physical LED display |

---

## HTTP API (Flask)

Flask serves HTTP endpoints for the admin UI and for Twilio webhooks. ESP32 fetches message history and config from Flask REST endpoints (`CONFIG_API_URL`, `MESSAGES_API_URL`).

### Endpoints

#### `GET /api/messages`

Fetch messages for the admin UI. Returns all messages with `received_at` strictly after `since`, ordered by `received_at` descending (most recent first).

**Request:**
```
GET /api/messages?since=2026-05-08T12:00:00Z
```

**Response** (200):

```json
[
  {
    "id": "abc-123-uuid",
    "sender": "+15551234567",
    "body": "Hello world",
    "received_at": "2026-05-08T12:05:00Z"
  }
]
```

**Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `since` | ISO 8601 timestamp | Return only messages strictly after this time. Omit to get all messages. |

#### `GET /api/config`

Fetch current config JSON.

**Response** (200):

```json
{
  "version": 1,
  "senders": [
    { "phone": "+15551234567", "name": "Alice" }
  ],
  "filters": [
    { "type": "keyword",  "pattern": "badword",      "action": "suppress" },
    { "type": "regex",    "pattern": "^\\s*$",        "action": "suppress" },
    { "type": "sender",   "pattern": "+15550001111",  "action": "suppress" },
    { "type": "message",  "pattern": "abc-123-uuid",  "action": "suppress" }
  ],
  "rendering": {
    "mode":  "scroll",
    "speed": 0.5,
    "color": 16711680
  },
  "sign": {
    "name": "Lindsay's Heart"
  },
  "timezone": "US/Pacific",
  "tz_offset_mins": 0
}
```

#### `PUT /api/config`

Update config (used by Flask admin UI; ESP32 does not write this).

**Request body:** Same JSON schema as above.

**Response** (200): `{"status": "ok"}`

#### `POST /api/messages`

Twilio webhook: receives inbound SMS.

**Request body:** `From=+15551234567&Body=hello&To=+15559999999` (form-encoded, Twilio format)

**Response** (200, TwiML):

```xml
<Response><Message>Lindsay's Heart got your message: hello</Message></Response>
```

#### `POST /api/messages/{id}/suppress`

Add a `type=message` filter rule for the given message UUID. Used by admin UI to suppress specific messages.

**Response** (200): `{"status": "ok", "filter_added": {"type": "message", "pattern": "...", "action": "suppress"}}`

> This is idempotent — suppressing an already-suppressed message is a no-op.

---

## ESP32 Communication Flow

### Boot

1. Connect to WiFi
2. Fetch message history from `MESSAGES_API_URL` (Flask REST) → populate in-memory ring buffer (UUID dedup)
3. Fetch config from `CONFIG_API_URL` (Flask REST) → store in memory
4. Subscribe to MQTT broker for real-time messages

### Main Loop

1. MQTT subscription receives new messages in real-time
2. On MQTT message: add to ring buffer (UUID dedup)
3. On config change via MQTT envelope: update in-memory config
4. `MessageManager.get_messages()` is called by the render loop to get the current queue

---

## Config Payload Schema

```json
{
  "version": 1,
  "senders": [
    { "phone": "+15551234567", "name": "Alice" }
  ],
  "filters": [
    { "type": "keyword",  "pattern": "badword",      "action": "suppress" },
    { "type": "regex",    "pattern": "^\\s*$",        "action": "suppress" },
    { "type": "sender",   "pattern": "+15550001111",  "action": "suppress" },
    { "type": "message",  "pattern": "abc-123-uuid",  "action": "suppress" }
  ],
  "rendering": {
    "mode":  "scroll",
    "speed": 0.5,
    "color": 16711680
  },
  "sign": {
    "name": "Lindsay's Heart"
  },
  "timezone": "US/Pacific",
  "tz_offset_mins": 0
}
```

Filter rule types:

| type | pattern matches | action |
|------|---------------|--------|
| `keyword` | Substring case-insensitive in message body | `suppress` |
| `regex` | Python regex match on message body | `suppress` |
| `sender` | Exact phone number (E.164) | `suppress` |
| `message` | Exact message UUID (from `messages.id`) | `suppress` |

---

## SQLite Schema

```sql
CREATE TABLE messages (
    id          TEXT PRIMARY KEY,    -- UUID v4
    sender      TEXT NOT NULL,       -- Phone number (E.164)
    body        TEXT NOT NULL,       -- Message text
    received_at TEXT NOT NULL        -- ISO 8601 timestamp
);

CREATE TABLE config (
    key    TEXT PRIMARY KEY,         -- e.g. "current"
    value  TEXT NOT NULL             -- JSON blob
);
```

The `config` table holds a single row with `key = "current"` and `value = <config JSON>`.

---

## Message Flow

### Inbound SMS

1. Visitor sends SMS to Twilio number
2. Twilio POSTs to Flask `/api/messages` with `From`, `Body`, `To`
3. Flask generates UUID, stores message to SQLite with `received_at`, logs to S3
4. Flask publishes `MessageEnvelope` to MQTT
5. Flask returns 200 TwiML with confirmation reply
6. ESP32 receives MQTT envelope, adds to ring buffer, triggers display update
7. Flask's own MQTT subscriber also receives the envelope and adds to its ring buffer

### ESP32 Message Retrieval

1. ESP32 fetches message history from Flask REST API on boot (`MESSAGES_API_URL`)
2. ESP32 fetches config from Flask REST API on boot (`CONFIG_API_URL`)
3. ESP32 subscribes to MQTT for real-time messages

---

## Shared Architecture

Both Flask and ESP32 share `lib_shared/`:

```
lib_shared/
├── models.py          # Message, SignConfig, FilterRule, RenderingSettings, etc.
├── messages.py        # FilteredMessages, InMemoryMessages
├── message_manager.py # MessageManager (dispatch + seed)
└── config_reader.py  # TOML + env config loader
```

Flask additionally uses `heart-message-manager/` modules directly:

```
heart-message-manager/
├── sqlite.py              # SQLite storage (Flask-only)
├── s3.py                 # S3 backup helpers (Flask-only)
├── server_time.py        # Time helpers with zoneinfo (Flask-only)
├── adafruit_mqtt_client.py  # Heroku MQTT subscriber
├── paho_mqtt_client.py      # Local dev MQTT subscriber
└── main.py               # Flask app
```

CircuitPython additionally uses `heart-matrix-controller/` modules:

```
heart-matrix-controller/
├── mqtt_client.py     # CircuitPython MQTT client (adafruit_io)
├── code.py            # Firmware entrypoint
└── ...
```

---

## Flask Admin UI

### 1. `/` — Dashboard

- Recent messages (last 20)
- Count: total, suppressed (per-filter), displayed
- Quick link to Settings

### 2. `/messages` — Message List

- Paginated table: UUID, sender (with name if in senders allowlist), body preview, timestamp
- Row actions: **Suppress** / **Unsuppress** (adds/removes `type=message` filter rule)

### 3. `/settings` — Settings

- Allowed senders: add/edit/remove `{name, phone}` pairs
- Rendering defaults: mode, speed, color
- Sign name
- Filter rules: add/delete suppression rules

### 4. `/preview` — Display Preview

- Shows exact list of messages that will display, using the shared `InMemoryMessages` filter logic

### 5. `/testing` — Testing

- Inject test messages and inspect system state

---

## File Structure

```
lindsay-50/
├── SPEC.md
├── README.md
├── CLAUDE.md
├── Procfile                        # Heroku startup
├── requirements-flask.txt     # Flask server deps
├── requirements-pi.txt        # Pi display device deps
├── requirements-provisioner.txt # Laptop provisioner deps
├── pyrightconfig.json
├── heart-message-manager/
│   ├── main.py
│   ├── sqlite.py                  # SQLite storage + S3 rebuild
│   ├── s3.py                      # S3 backup helpers
│   ├── server_time.py             # Time helpers (zoneinfo)
│   ├── adafruit_mqtt_client.py    # Heroku MQTT subscriber
│   ├── paho_mqtt_client.py        # Local dev MQTT subscriber
│   ├── templates/
│   ├── settings.toml.example
│   └── db.sqlite                  # SQLite DB (gitignored)
├── heart-matrix-controller/
│   ├── code.py                    # CircuitPython firmware
│   ├── mqtt_client.py             # CircuitPython MQTT client
│   ├── scroller.py
│   ├── fireworks.py
│   ├── flame.py
│   └── settings.toml.example
└── lib_shared/
    ├── models.py                  # Shared data models
    ├── messages.py                # In-memory message ring buffer
    ├── message_manager.py         # MessageManager (dispatch + seed)
    └── config_reader.py           # TOML + env config loader
```

---

## What's Working

- [x] SMS → Twilio webhook → Flask `/api/messages` endpoint
- [x] Flask stores messages to SQLite + S3 backup
- [x] Flask rebuilds SQLite from S3 on startup
- [x] Flask publishes `MessageEnvelope` to MQTT broker
- [x] Flask subscribes to MQTT (MessageManager ring buffer)
- [x] ESP32 connects to WiFi
- [x] ESP32 fetches message history and config from Flask REST API on boot
- [x] ESP32 subscribes to MQTT for real-time messages
- [x] ESP32 renders Scroller, Fireworks effects
- [x] EffectCoordinator fades between effects on new message
- [x] Flask admin UI: Dashboard, Messages, Settings, Preview, Testing
- [x] Filter rules: keyword, regex, sender, message suppression
- [x] `lib_shared/` code works on both Flask and CircuitPython

## TODO

- [ ] **ESP32 message pruning**: In-memory ring buffer grows indefinitely — when and how to prune old messages?
- [ ] **Adafruit IO rate limits**: How does fetching message history work — any rate limits or pagination?
- [ ] **Config change detection**: ESP32 polls Flask HTTP for config changes. How often should it poll?
- [ ] **Weekly re-publish**: How to implement periodic re-publish of messages to Adafruit IO to prevent aging?

---

## Open Questions

1. **ESP32 message pruning**: In-memory ring buffer grows indefinitely — when and how to prune old messages?
2. **Adafruit IO rate limits**: How does fetching message history work — any rate limits or pagination?
3. **Config change detection**: ESP32 polls Flask HTTP for config changes. How often should it poll?
4. **S3 log format**: JSONL per message, or something else? How to handle rebuild from S3?
5. **Weekly re-publish**: How to implement periodic re-publish of messages to Adafruit IO to prevent aging?
