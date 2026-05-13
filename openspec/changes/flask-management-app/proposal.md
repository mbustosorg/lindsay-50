## Why

The heart sign project needs a persistent backend: inbound SMS must be stored and managed, and operators need a way to configure filters, allowed senders, and display settings. Right now messages go directly from Flask to Adafruit IO with no persistence and no admin UI. This change adds the management layer.

## What Changes

- Flask app gains SQLite persistence with S3 message logging for durable backup
- Shared `lib/` provides message storage, config storage, and filtering for Flask
- Admin UI with pages: Dashboard, Message list, Filter rules, Settings, Preview
- Twilio webhook refactored to store messages and log to S3 before publishing to Adafruit IO
- API endpoints for config and message management

## Capabilities

### New Capabilities

- `message-storage`: SQLite-backed message persistence with `id` (UUID), `sender` (phone), `body`, `received_at`. All messages logged to S3.
- `config-storage`: JSON config stored in SQLite `config` table, supports `allowed_senders`, `filters`, `rendering`, `sign` sections.
- `message-filtering`: Filter engine supporting `keyword`, `regex`, `sender`, and `message` (UUID) rules. `display_list()` returns only non-suppressed messages in order.
- `admin-ui`: Web-based UI for managing messages, filter rules, and settings. Includes a Preview page that shows the exact output of `display_list()`.
- `twilio-webhook`: Flask endpoint `POST /api/messages` that stores to SQLite, logs to S3, publishes to Adafruit IO, and returns confirmation.

## Impact

- New files: `lib/models.py`, `lib/storage.py`, `lib/filters.py`, `Procfile`, admin UI templates
- Modified: `heart-message-manager/main.py` refactored to use `lib/storage`
- Dependencies: AWS S3 bucket configured for message logging
- ESP32: ESP32 uses Adafruit IO for message history and real-time delivery; shared `filters.py` provides identical filter logic
