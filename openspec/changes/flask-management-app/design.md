## Context

The heart sign currently has a Flask app that receives Twilio webhooks and publishes raw message text to Adafruit IO MQTT. There is no persistence, no config management, and no admin UI. The ESP32 consumes messages from Adafruit IO.

We need to add persistence and a management UI to Flask before the ESP32 communication layer can be finalized.

The `lib/` directory is shared between Flask and ESP32 so both sides run identical storage and filter logic.

## Goals / Non-Goals

**Goals:**
- Persistent SQLite storage for all inbound messages on Flask, replicated to R2 via Litestream
- Config stored in SQLite as JSON, readable by both Flask and ESP32
- Filter engine (`lib/filters.py`) used identically by Flask admin preview and ESP32 render loop
- Admin UI for managing messages, filters, settings
- Twilio webhook that stores before returning TwiML

**Non-Goals:**
- ESP32-side code changes (communication layer: MQTT vs HTTP undecided)
- ESP32 persistence (future work)
- Config push mechanism (future work; polling from ESP32 handles it either way)

## Decisions

### SQLite + Litestream over separate DB service

**Decision:** Use SQLite with Litestream streaming WAL to Cloudflare R2, rather than a managed database service.

**Rationale:** Zero cost, no separate service to manage, Litestream handles replication transparently. Flask reads/writes SQLite normally; Litestream is a sidecar process.

**Alternative considered:** Managed PostgreSQL on Render ($7+/mo) or Supabase. Overkill for low-traffic app; adds cost and complexity.

### `lib/` as a shared module for both Flask and ESP32

**Decision:** Write `lib/models.py`, `lib/storage.py`, and `lib/filters.py` once, importable from both Flask and CircuitPython.

**Rationale:** The Flask admin Preview page must show exactly what the ESP32 will display. If filter logic is duplicated, it will diverge. Both sides must agree on schema, storage API, and filter behavior.

**Constraint:** CircuitPython has limited stdlib — no `uuid`, no `json` (use `ujson` or string manipulation), no `re` (use `ure` instead). `lib/filters.py` must use `ure` for regex. `lib/storage.py` must use `adafruit_sqlite` on ESP32 and built-in `sqlite3` on Flask.

### Flask as sole source of truth for config

**Decision:** Config lives in Flask's SQLite and is only read by ESP32 (no push). ESP32 polls `GET /api/config` to detect changes.

**Rationale:** Simplifies ESP32 logic. Whatever the communication layer (MQTT or HTTP), the config-fetch mechanism can piggyback on the same connection.

**Alternative considered:** ESP32 pushes config changes to Flask. Adds complexity; central-wins is simpler for v1.

### Admin UI served by Flask directly

**Decision:** Flask serves HTML templates directly (no separate static SPA).

**Rationale:** Simpler deployment — one service. Low UI complexity (tables + forms). Jinja2 templates are sufficient.

**Alternative considered:** Separate React/Vue SPA. Overkill for this use case.

## Risks / Trade-offs

**[Risk] ESP32 CircuitPython SQLite library maturity**
→ `adafruit_sqlite` is in beta. If it proves unreliable, fall back to JSONL on ESP32 (sacrificing per-message suppress).

**[Risk] Render free tier cold start**
→ Twilio webhook timeout is 15s; Render cold start can exceed that. Mitigated by UptimeRobot pinging every 5 min to keep dyno awake. Acceptable for v1.

**[Risk] R2 write budget**
→ Litestream generates WAL writes. At 1 message/min and 5-min sync interval, ~10k writes/month is well under R2's 1M free writes.

**[Risk] Config schema changes require migration**
→ `version` field in config JSON allows future migration logic. For v1, just overwrite.

## Open Questions

1. What is the practical long-poll timeout limit on Render free tier? Affects ESP32 reconnect frequency.
2. Should `lib/` use `ujson` or manual string formatting for JSON on ESP32?
3. Do we need a Flask-side allowlist check (reject non-allowed senders at webhook time), or just store everything and let filtering handle it?
