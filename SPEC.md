# Heart Matrix Sign — Project Specification

## Overview

An ESP32-based programmable LED matrix sign (64x64, 2-tile HUB75) that displays SMS messages sent by visitors. A Twilio webhook receiver (Flask on Heroku) processes incoming SMS, stores it to SQLite, logs to S3, and publishes to Adafruit IO via MQTT. The ESP32 subscribes to Adafruit IO MQTT for real-time messages, fetches config via HTTP, stores messages locally with UUID deduplication, filters them, and renders animated effects on the LED matrix.

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
                        │  │   lib/     │                          │
                        │  └────────────┘                          │
                        │        │                                 │
                        │        │ publishes                       │
                        │        ▼                                 │
                        │  ┌────────────────────┐    ┌───────────┐ │
                        │  │    Adafruit IO     │◀───│  ESP32    │ │
                        │  │  (transport only)  │    │  Matrix   │ │
                        │  └────────────────────┘    └───────────┘ │
                        │        ▲                                 │
                        │        │ MQTT + HTTP                     │
                        └────────┼─────────────────────────────────┘
                                 │
                       ┌─────────┴─────────────────────────────────┐
                       │   ESP32 (CircuitPython)                   │
                       │   • MQTT subscribe (realtime).            │
                       │   • HTTP fetch history + config.          │
                       │   • In-memory dict (UUID dedup).          │
                       │   • Rebuilds from Adafruit IO on boot.    │
                       └───────────────────────────────────────────┘
```

### Communication Architecture

- **Flask**: S3 is the ultimate source of truth. On restart, Flask seeds SQLite from S3. For normal operation, Flask uses its local SQLite.
- **ESP32**: Adafruit IO is the source of truth. ESP32 subscribes to Adafruit IO MQTT for real-time messages, stores them in an in-memory dict (UUID deduplication), and applies filters at render time. On boot, ESP32 fetches message history from Adafruit IO via HTTP to warm its in-memory store.
- **S3 message logging**: All inbound messages are logged to S3 as a durable backup. Flask rebuilds from S3 on restart.
- **Adafruit IO as transport**: MQTT carries real-time messages to ESP32; HTTP carries message history and config fetches.
- **Shared `filters.py`**: Both sides use the same filter logic for consistency between admin UI preview and ESP32 rendering.

---

## Tech Stack

| Layer | Technology | Responsibility |
|-------|------------|----------------|
| SMS | Twilio | Receives SMS from visitors, sends webhook to Flask |
| Webhook Receiver | Flask (Python) on Heroku | Receives webhook, logs to S3, stores to SQLite, publishes to Adafruit IO |
| Database | SQLite | Local message/config storage on Flask |
| Object Storage | AWS S3 | Durable message log, Flask rebuilds from S3 on restart |
| Sign Controller | ESP32 (CircuitPython) | Subscribes to Adafruit IO MQTT, HTTP fetch for history/config, in-memory message store, filters at render time |
| Display | 64x64 HUB75 matrix (2× 64x32 tiles) | Physical LED display |

### Hosting Details

| Component | Choice |
|-----------|--------|
| Web host | Heroku |
| Database | SQLite |
| Object storage | AWS S3 |

---

## HTTP API (Flask)

Flask serves HTTP endpoints for the admin UI and for Twilio webhooks. ESP32 does not communicate with Flask over HTTP — it uses Adafruit IO MQTT (real-time) and Adafruit IO HTTP (message history and config).

### Endpoints

#### `GET /api/messages`

Fetch messages for the admin UI. Returns all messages with `received_at` strictly after `since`, ordered by `received_at` descending (most recent first).

**Request:**
```
GET /api/messages?since=2026-05-08T12:00:00Z
```

**Response** (200):

```json
{
  "messages": [
    {
      "id": "abc-123-uuid",
      "sender": "+15551234567",
      "body": "Hello world",
      "received_at": "2026-05-08T12:05:00Z"
    }
  ]
}
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
  "allowed_senders": [
    { "name": "Alice", "phone": "+15551234567" }
  ],
  "filters": [
    { "type": "keyword",  "pattern": "badword",      "action": "suppress" },
    { "type": "regex",    "pattern": "^\\s*$",        "action": "suppress" },
    { "type": "sender",   "pattern": "+15550001111",  "action": "suppress" },
    { "type": "message",  "pattern": "abc-123-uuid",  "action": "suppress" }
  ],
  "rendering": {
    "mode":  "scroll",
    "speed": 0.04,
    "color": 16711680
  },
  "sign": {
    "name": "Lindsay's Heart"
  }
}
```

#### `PUT /api/config`

Update config (used by Flask admin UI; ESP32 does not write this).

**Request body:** Same JSON schema as above.

**Response** (200): `{"ok": true}`

#### `POST /api/messages`

Twilio webhook: receives inbound SMS. (Also used by Flask internally after storing to SQLite.)

**Request body:** `From=+15551234567&Body=hello&To=+15559999999` (form-encoded, Twilio format)

**Response** (200, TwiML):

```xml
<Response><Message>Lindsay's Heart got your message: hello</Message></Response>
```

#### `POST /api/messages/{id}/suppress`

Add a `type=message` filter rule for the given message UUID. Used by admin UI to suppress specific messages.

**Response** (200): `{"ok": true}`

> This is idempotent — suppressing an already-suppressed message is a no-op.

---

## ESP32 Communication Flow

### Boot

1. Connect to WiFi
2. Fetch message history from Adafruit IO HTTP API → populate in-memory message dict (UUID dedup)
3. Fetch config from Adafruit IO HTTP API → store in memory
4. Subscribe to Adafruit IO MQTT for real-time messages

### Main Loop

1. MQTT subscription receives new messages in real-time
2. On MQTT message: add to in-memory dict (UUID dedup), update `last_seen_at`
3. On config change: re-fetch config from Adafruit IO HTTP
4. `filters.display_list()` is called by the render loop to get the current queue

---

## Config Payload Schema

```json
{
  "version": 1,
  "allowed_senders": [
    { "name": "Alice", "phone": "+15551234567" }
  ],
  "filters": [
    { "type": "keyword",  "pattern": "badword",      "action": "suppress" },
    { "type": "regex",    "pattern": "^\\s*$",        "action": "suppress" },
    { "type": "sender",   "pattern": "+15550001111",  "action": "suppress" },
    { "type": "message",  "pattern": "abc-123-uuid",  "action": "suppress" }
  ],
  "rendering": {
    "mode":  "scroll",
    "speed": 0.04,
    "color": 16711680
  },
  "sign": {
    "name": "Lindsay's Heart"
  }
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

Both Flask and ESP32 run identical SQLite schemas (from `lib/models.py`).

```sql
CREATE TABLE messages (
    id          TEXT PRIMARY KEY,    -- UUID v4
    sender      TEXT NOT NULL,       -- Phone number (E.164)
    body        TEXT NOT NULL,       -- Message text
    received_at TEXT NOT NULL        -- ISO 8601 timestamp
);

CREATE TABLE config (
    key    TEXT PRIMARY KEY,         -- e.g. "current"
    value  TEXT NOT NULL            -- JSON blob
);
```

The `config` table holds a single row with `key = "current"` and `value = <config JSON>`.

---

## Message Flow

### Inbound SMS

1. Visitor sends SMS to Twilio number
2. Twilio POSTs to Flask `/api/messages` with `From`, `Body`, `To`
3. Flask generates UUID, stores message to SQLite with `received_at`
4. Flask returns 200 TwiML with confirmation reply

### ESP32 Message Retrieval

1. ESP32 fetches message history from Adafruit IO HTTP on boot
2. ESP32 subscribes to Adafruit IO MQTT for real-time messages
3. Messages are stored in an in-memory dict with UUID deduplication

---

## Shared Architecture

Both Flask and ESP32 share `lib/filters.py`:

```
lib/
├── __init__.py
├── models.py       # Message and Config dataclasses
├── storage.py      # Flask storage implementation (SQLite)
└── filters.py      # apply(), display_list() — same filter logic on both sides
```

**Why share filters.py?** The Flask admin UI preview page shows "what the sign will display" by running the exact same filter logic the ESP32 uses. Both sides must agree on which messages pass the filter rules.

**Storage:** Flask uses SQLite via `lib/storage.py`. ESP32 uses an in-memory dict for messages and stores config in memory.

---

## Flask Admin UI

### 1. `/` — Dashboard

- Recent messages (last 20)
- Count: total, suppressed (per-filter), displayed
- Quick link to Config and Filters

### 2. `/messages` — Message List

- Paginated table: UUID, sender (with name if in allowed_senders), body preview, timestamp
- Row actions: **Suppress** / **Unsuppress** (adds/removes `type=message` filter rule)
- "View display list" link → shows filtered output

### 3. `/filters` — Filter Rules

- List all filter rules
- Add rule: type, pattern, action
- Delete rule

### 4. `/settings` — Settings

- Allowed senders: add/edit/remove `{name, phone}` pairs
- Rendering defaults: mode, speed, color
- Sign name

### 5. `/preview` — Display Preview

- Shows exact list of messages the ESP32 will display, using `filters.display_list()` with the current config
- Same filtering logic as ESP32 — no surprises

---

## File Structure

```
lindsay-50/
├── SPEC.md
├── Procfile                        # Heroku startup
├── lib/                           # Shared code (Flask)
│   ├── __init__.py
│   ├── models.py                  # Message/Config dataclasses
│   ├── storage.py                 # Flask SQLite storage
│   └── filters.py                # apply(), display_list()
├── heart-message-manager/
│   ├── __init__.py
│   ├── main.py                   # Flask app (Twilio webhook, admin UI)
│   └── settings.toml.example
└── heart-matrix-controller/
    ├── code.py                   # CircuitPython firmware (MQTT + HTTP via Adafruit IO)
    ├── scroller.py               # Text scroll effect
    ├── fireworks.py              # Firework particle effect
    ├── flame.py                  # Flame effect (hidden)
    ├── settings.toml.example     # Config template
    └── filters.py               # Same filter logic as Flask lib/filters.py
```

---

## What's Working

- [x] SMS → Twilio webhook → Flask `/api/messages` endpoint
- [x] ESP32 connects to WiFi
- [x] ESP32 renders Scroller, Fireworks effects
- [x] EffectCoordinator fades between effects on new message

## TODO

- [ ] **Flask SQLite + S3 logging**: Flask stores messages in SQLite, logs to S3, rebuilds from S3 on restart
- [ ] **Heroku deployment**: Procfile, config vars setup
- [ ] **`lib/` shared code**: models.py, storage.py, filters.py for Flask
- [ ] **ESP32 MQTT subscribe**: Subscribe to Adafruit IO MQTT for real-time messages
- [ ] **ESP32 in-memory store**: Message dict with UUID deduplication, fixed-size retention
- [ ] **ESP32 history fetch**: Fetch message history from Adafruit IO HTTP on boot
- [ ] **`type=message` filter**: Suppress specific message by UUID
- [ ] **Flask admin UI**: Five pages — Dashboard, Messages, Filters, Settings, Preview
- [ ] **Adafruit IO auth delegation**: Allow Lindsay to log in with Adafruit IO credentials to modify config

---

## Open Questions

1. **ESP32 message pruning**: In-memory dict grows indefinitely — when and how to prune old messages?
2. **Adafruit IO rate limits**: How does fetching message history work — any rate limits or pagination?
3. **Config change detection**: ESP32 polls Adafruit IO HTTP for config changes. How often should it poll?
4. **S3 log format**: JSONL per message, or something else? How to handle rebuild from S3?
5. **Weekly re-publish**: How to implement periodic re-publish of messages to Adafruit IO to prevent aging?
