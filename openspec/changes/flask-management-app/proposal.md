## Why

The heart sign project needs a persistent backend: inbound SMS must be stored and managed, and operators need a way to configure filters, allowed senders, and display settings. Right now messages go directly from Flask to Adafruit IO with no persistence and no admin UI. This change adds the management layer.

This work is decoupled from the ESP32 communication layer (MQTT vs HTTP), so it can proceed regardless of that decision.

## What Changes

- Flask app gains SQLite persistence via Litestream → Cloudflare R2
- Shared `lib/` provides message storage, config storage, and filtering for both Flask and ESP32
- Admin UI with pages: Dashboard, Message list, Filter rules, Settings, Preview
- Twilio webhook refactored to store messages before publishing
- API endpoints for config and message management

## Capabilities

### New Capabilities

- `message-storage`: SQLite-backed message persistence with `id` (UUID), `sender` (phone), `body`, `received_at`. Litestream replicates to R2.
- `config-storage`: JSON config stored in SQLite `config` table, supports `allowed_senders`, `filters`, `rendering`, `sign` sections.
- `message-filtering`: Filter engine supporting `keyword`, `regex`, `sender`, and `message` (UUID) rules. `display_list()` returns only non-suppressed messages in order.
- `admin-ui`: Web-based UI for managing messages, filter rules, and settings. Includes a Preview page that shows the exact output of `display_list()`.
- `twilio-webhook`: Flask endpoint `POST /api/messages` that stores to SQLite and returns TwiML. `POST /api/messages/{id}/suppress` adds a per-message suppress rule.

## Impact

- New files: `lib/models.py`, `lib/storage.py`, `lib/filters.py`, `Procfile`, `litestream.yml`, admin UI templates
- Modified: `heart-sms-receiver/main.py` refactored to use `lib/storage`
- Dependency: Cloudflare R2 bucket + Litestream configured for deployment
- ESP32: No changes in this issue; `lib/` is written to be compatible with both Flask and CircuitPython
