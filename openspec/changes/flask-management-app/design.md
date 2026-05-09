## Context

The heart sign currently has a Flask app that receives Twilio webhooks and publishes raw message text to Adafruit IO MQTT. There is no persistence, no config management, and no admin UI. The ESP32 consumes messages from Adafruit IO.

We need to add persistence and a management UI to Flask before the ESP32 communication layer can be finalized.

## Goals / Non-Goals

**Goals:**
- Persistent SQLite storage for all inbound messages on Flask, with S3 logging for durability
- Config stored in SQLite as JSON, readable by Flask admin UI
- Filter engine (`lib/filters.py`) used identically by Flask admin preview and ESP32 render loop
- Admin UI for managing messages, filters, settings
- Twilio webhook that stores to SQLite, logs to S3, and publishes to Adafruit IO

**Non-Goals:**
- Flask does not serve message history to ESP32 (ESP32 uses Adafruit IO for that)
- ESP32 uses in-memory dict for message storage, not SQLite
- Config push mechanism (ESP32 polls Adafruit IO HTTP for config changes)

## Decisions

### SQLite + S3 logging over Litestream

**Decision:** Use SQLite for Flask's local storage with S3 as a durable message log, rather than Litestream replicating to R2.

**Rationale:** S3 provides a simple, durable backup of all inbound messages. Flask rebuilds SQLite from S3 on restart. No need for continuous replication — just append-only logging.

**Alternative considered:** Litestream streaming WAL to R2. More complex setup; S3 logging is simpler.

### `lib/` filter logic shared between Flask and ESP32

**Decision:** Write `lib/filters.py` once, importable from both Flask and CircuitPython.

**Rationale:** The Flask admin Preview page must show exactly what the ESP32 will display. If filter logic is duplicated, it will diverge. Both sides must agree on filter behavior.

**Constraint:** ESP32 uses in-memory dict for messages, not SQLite. Filters operate on message collections, not storage backends.

### Flask publishes to Adafruit IO; ESP32 subscribes via Adafruit IO

**Decision:** Flask publishes messages to Adafruit IO via MQTT. ESP32 subscribes to Adafruit IO MQTT for real-time messages and fetches message history via Adafruit IO HTTP on boot.

**Rationale:** This keeps the communication architecture simple. ESP32 uses Adafruit IO as its source of truth. Flask uses S3 as its source of truth.

### Admin UI served by Flask directly

**Decision:** Flask serves HTML templates directly (no separate static SPA).

**Rationale:** Simpler deployment — one service. Low UI complexity (tables + forms). Jinja2 templates are sufficient.

**Alternative considered:** Separate React/Vue SPA. Overkill for this use case.

## Risks / Trade-offs

**[Risk] S3 log format and rebuild strategy**
→ Need to define JSONL format for S3 logging. Rebuild from S3 on Flask restart must be reliable.

**[Risk] ESP32 in-memory store size**
→ In-memory dict grows indefinitely. Need a fixed-size retention policy (e.g., last 100 messages).

**[Risk] Adafruit IO message aging**
→ Adafruit IO may not retain messages indefinitely. ESP32 fetches history on boot to catch up, but if messages age out, they could be lost. Mitigated by periodic re-publish.

## Open Questions

1. What is the S3 log format? JSONL per message, or batched uploads?
2. How does ESP32 handle Adafruit IO rate limits when fetching history?
3. How often should ESP32 poll Adafruit IO HTTP for config changes?
4. What is the fixed-size retention policy for ESP32 in-memory store?
