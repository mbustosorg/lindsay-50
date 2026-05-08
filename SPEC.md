# Heart Matrix Sign — Project Specification

## Overview

An ESP32-based programmable LED matrix sign (64x64, 2-tile HUB75) that displays SMS messages sent by visitors. A Twilio webhook receiver (Flask on Render) processes incoming SMS and stores it to SQLite (replicated to Cloudflare R2 via Litestream). The ESP32 long-polls Flask via HTTP to receive messages and config, filters them, and renders animated effects on the LED matrix.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                                           CLOUD                                          │
│                                                                                         │
│  ┌──────────┐      ┌─────────────────┐      ┌────────────────┐                        │
│  │  Twilio  │─────▶│  Flask/Render  │      │  Cloudflare   │                        │
│  │  (SMS)   │      │  (web service) │      │      R2       │                        │
│  └──────────┘      └────────┬────────┘      └───────┬────────┘                        │
│                               │                       │                                 │
│                               │  SQLite               │  Litestream WAL                │
│                               │  (lib/)               └─────────────────────────────────┤
│                               └─────────────────────────────────────────────────────────┘
│                                                                                          │
│                               HTTP API (Flask):                                           │
│                               • GET  /api/messages?since={t}  (long-poll)                 │
│                               • GET  /api/config                                          │
│                               • PUT  /api/config                                          │
│                               • POST /api/messages/{id}/suppress                          │
│                                                                                          │
│                               ──────────────────────────────────────────────────────────
│                                                  │ HTTP (WiFi)
│                                                  ▼
│                                       ┌─────────────────────┐
│                                       │   ESP32 + HUB75    │
│                                       │   LED Matrix        │
│                                       │   (CircuitPython)   │
│                                       └─────────────────────┘
│                                                                                          │
│                                       Local storage (SQLite):                              │
│                                       • messages.db    • config.db                         │
```

### Changes from prior design

- **No Adafruit IO MQTT**. Adafruit IO does not support retained messages, so any message published while the ESP32 was offline would be permanently lost. HTTP long-polling fixes this — the server is the source of truth and the ESP32 can always catch up on reconnect.
- **Flask is the sole API** for both admin UI and ESP32 communication.
- **Shared `lib/`** is still the right structure — both sides run SQLite and the same filter logic.

---

## Tech Stack

| Layer | Technology | Responsibility |
|-------|------------|----------------|
| SMS | Twilio | Receives SMS from visitors, sends webhook to Flask |
| Webhook Receiver | Flask (Python) on Render | Validates sender, stores to SQLite, serves HTTP API, returns TwiML |
| Database | SQLite + Litestream | Persistent message storage, replicated to R2 |
| Object Storage | Cloudflare R2 | Offsite replica of SQLite (Litestream) |
| Sign Controller | ESP32 (CircuitPython) | Long-polls Flask for messages and config, stores locally, filters at read time, renders |
| Display | 64x64 HUB75 matrix (2× 64x32 tiles) | Physical LED display |
| Keep-alive | UptimeRobot | Pings Render every 5 min to prevent cold start |

### Hosting Details

| Component | Choice | Why |
|-----------|--------|-----|
| Web host | Render free tier | 750 hrs/month, git-push deploys, managed TLS |
| Database | SQLite + Litestream | Zero-cost, no separate service, minimal ops |
| Object storage | Cloudflare R2 | 10 GB free, 1M writes/month free — better than S3/GCS for Litestream |
| Keep-alive | UptimeRobot free | Prevents Render's 15-min idle spin-down (Twilio webhook timeout is 15s) |
| Total cost | **$0/month** | |

---

## HTTP API (Flask ↔ ESP32)

### Why HTTP over MQTT

Adafruit IO MQTT has no retained messages, no offline queuing. Any message published while the ESP32 is offline is permanently lost. The ESP32 cannot maintain a message buffer because it has no reliable way to know what it missed.

HTTP long-polling solves this: the server holds the connection open until there are new messages (or a timeout). When the ESP32 reconnects after being offline, it sends `?since=<last_seen_timestamp>` and the server returns all messages it missed.

### Endpoints

#### `GET /api/messages`

Long-poll for new messages. Returns all messages with `received_at` strictly after `since`.

**Request:**
```
GET /api/messages?since=2026-05-08T12:00:00Z
```

**Response** (200, held open until a message arrives or timeout):

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

**Timeout behavior:**
- If no messages arrive before the timeout (~30s), return `{"messages": []}` with HTTP 200.
- ESP32 treats empty array as "no new messages, retry".

**Parameters:**

| Param | Type | Description |
|-------|------|-------------|
| `since` | ISO 8601 timestamp | Return only messages strictly after this time. Omit to get all messages (used on initial fetch). |

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

1. Load config from local SQLite (if any)
2. Connect to WiFi
3. `GET /api/config` → overwrite local config
4. Record `last_seen_at = max(message.received_at)` from config response's messages (or current time if none)

### Main Loop

1. `GET /api/messages?since={last_seen_at}` (long-poll, ~30s timeout)
2. On response with messages: insert each into local SQLite, update `last_seen_at`
3. On response with empty array: no new messages, retry poll immediately
4. On network error: retry with backoff
5. `filters.display_list()` is called by the render loop to get the current queue

### On Config Change (polling-based detection)

Every long-poll response includes the current config (or the ESP32 can re-poll `GET /api/config` periodically, e.g. every 60s). If config has changed (compare `version` or hash), overwrite local config SQLite.

> A push-based alternative (e.g. Server-Sent Events) would require Flask to hold a connection open indefinitely, complicating Render deployment. Polling is simpler and sufficient at low message rates.

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

### ESP32 Long-Poll

1. ESP32 sends `GET /api/messages?since={last_seen_at}`
2. If no new messages: Flask holds connection open (~30s timeout)
3. When Flask receives an SMS: it stores to SQLite, then returns the new message(s) to the waiting ESP32
4. ESP32 inserts into local SQLite, updates `last_seen_at`, continues polling

---

## Shared Architecture

Both Flask and ESP32 share code from a `lib/` directory:

```
lib/
├── __init__.py
├── models.py       # SQLite schema, Message + Config data classes
├── storage.py      # put_message(), get_messages(), put_config(), get_config()
└── filters.py      # apply_filters(), display_list() — applied identically by both
```

**Why share filters.py?** The Flask admin UI preview page shows "what the sign will display" by running the exact same filter logic the ESP32 uses. Both sides must agree on which messages pass the filter rules.

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
├── Procfile                        # Render startup
├── litestream.yml                  # Litestream → R2 config
├── lib/                           # Shared code (Flask + CircuitPython)
│   ├── __init__.py
│   ├── models.py                  # SQLite schema, Message/Config dataclasses
│   ├── storage.py                 # put_message(), get_messages_since(), put_config(), get_config()
│   └── filters.py                # apply_filters(), display_list()
├── heart-sms-receiver/
│   ├── __init__.py
│   ├── main.py                   # Flask app (HTTP API + admin UI)
│   └── requirements.txt
└── heart-matrix-controller/
    ├── code.py                   # CircuitPython firmware (HTTP long-poll loop)
    ├── scroller.py               # Text scroll effect
    ├── fireworks.py              # Firework particle effect
    ├── flame.py                  # Flame effect (hidden)
    ├── settings.toml.example     # Config template
    └── db.py                    # ESP32 DB init (imports lib/, uses adafruit_sqlite)
```

---

## What's Working

- [x] SMS → Twilio webhook → Flask `/api/messages` endpoint
- [x] ESP32 connects to WiFi
- [x] ESP32 renders Scroller, Fireworks effects
- [x] EffectCoordinator fades between effects on new message

## TODO

- [ ] **SQLite + Litestream + R2**: Flask stores messages in SQLite, Litestream replicates to R2
- [ ] **Render deployment**: Procfile, litestream.yml, UptimeRobot setup
- [ ] **`lib/` shared code**: models.py, storage.py, filters.py for both Flask and ESP32
- [ ] **ESP32 HTTP long-poll**: Replace MQTT with `adafruit_requests` long-poll loop
- [ ] **ESP32 SQLite**: Local SQLite via `adafruit_sqlite` (or similar CircuitPython SQLite library)
- [ ] **`type=message` filter**: Suppress specific message by UUID
- [ ] **Flask admin UI**: Five pages — Dashboard, Messages, Filters, Settings, Preview
- [ ] **ESP32 config polling**: Re-poll `GET /api/config` periodically to detect changes

---

## Open Questions

1. **ESP32 SQLite library**: Does CircuitPython have a viable SQLite library for ESP32? (`adafruit_sqlite` is in beta; alternatives?)
2. **ESP32 message pruning**: SQLite grows indefinitely on the ESP32 — when and how to prune old messages?
3. **Render cold start**: UptimeRobot pings every 5 min — Twilio times out at 15s; acceptable risk?
4. **Litestream sync interval**: 5 min default — R2 write budget is 1M/month, is this a real concern at low traffic?
5. **Config push vs. poll**: Should ESP32 detect config changes via periodic `GET /api/config` polling, or is version-checking in each long-poll response sufficient?
6. **Long-poll timeout**: Render's idle timeout may be shorter than our desired long-poll duration — what is the practical limit?
