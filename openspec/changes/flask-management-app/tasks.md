## 1. Deployment Infrastructure

- [ ] 1.1 Create `Procfile` with `web: litestream restore -if-replica-exists /app/db.sqlite && litestream replicate -exec "flask run --host=0.0.0.0"`
- [ ] 1.2 Create `litestream.yml` with R2 bucket configuration
- [ ] 1.3 Create `heart-sms-receiver/requirements.txt` with flask, adafruit-io, litestream, jinja2
- [ ] 1.4 Create Cloudflare R2 bucket and configure credentials

## 2. Shared Library (`lib/`)

- [ ] 2.1 Create `lib/__init__.py`
- [ ] 2.2 Create `lib/models.py` with `Message` dataclass (id, sender, body, received_at) and `Config` dataclass
- [ ] 2.3 Create `lib/storage.py` with `init_db()`, `put_message()`, `get_messages_since()`, `get_all_messages()`, `get_message()`, `put_config()`, `get_config()`
- [ ] 2.4 Create `lib/filters.py` with `apply(message, config)` and `display_list(messages, config)`, using Python `re` module

## 3. Flask App Refactor

- [ ] 3.1 Refactor `heart-sms-receiver/main.py` to import from `lib/storage`
- [ ] 3.2 Update Twilio webhook to use `storage.put_message()` before returning TwiML
- [ ] 3.3 Add `GET /api/messages?since={timestamp}` endpoint returning JSON array
- [ ] 3.4 Add `GET /api/config` returning current config JSON
- [ ] 3.5 Add `PUT /api/config` accepting full config JSON, storing and publishing
- [ ] 3.6 Add `POST /api/messages/{id}/suppress` adding `type=message` filter rule
- [ ] 3.7 Add `POST /api/messages/{id}/unsuppress` removing `type=message` filter rule

## 4. Admin UI

- [ ] 4.1 Create `heart-sms-receiver/templates/base.html` with Bootstrap layout
- [ ] 4.2 Create `GET /` (Dashboard) showing recent messages and counts
- [ ] 4.3 Create `GET /messages` with pagination (50 per page), suppress/unsuppress buttons
- [ ] 4.4 Create `GET/POST /filters` listing, adding, and deleting filter rules
- [ ] 4.5 Create `GET/POST /settings` for allowed_senders, rendering, sign name
- [ ] 4.6 Create `GET /preview` showing `filters.display_list()` output
- [ ] 4.7 Wire settings/filters/message actions to trigger config publish

## 5. Config Publish (Interface Only)

- [ ] 5.1 Stub publish function in `lib/` (publish to MQTT topic OR make HTTP POST to ESP32 endpoint — pending communication decision)
- [ ] 5.2 Call stub publish function after any config change via admin UI
