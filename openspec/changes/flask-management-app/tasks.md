## 1. Deployment Infrastructure

- [ ] 1.1 Create `Procfile` with `web: flask run --host=0.0.0.0`
- [ ] 1.2 Create `heart-sms-receiver/requirements.txt` with flask, adafruit-io, boto3, jinja2
- [ ] 1.3 Configure AWS S3 bucket for message logging
- [ ] 1.4 Create S3 backup functions: log_message(), save_config_snapshot(), load_latest_config()

## 2. Shared Library (`lib/`)

- [ ] 2.1 Create `lib/__init__.py`
- [ ] 2.2 Create `lib/models.py` with `Message` dataclass (id, sender, body, received_at) and `Config` dataclass
- [ ] 2.3 Create `lib/storage.py` with `init_db()`, `put_message()`, `get_messages_since()`, `get_all_messages()`, `get_message()`, `put_config()`, `get_config()`
- [ ] 2.4 Create `lib/filters.py` with `apply(message, config)` and `get_messages(messages, config, include_filtered=False, since=None)`, using Python `re` module

## 3. Flask App Refactor

- [ ] 3.1 Refactor `heart-sms-receiver/main.py` to import from `lib/storage`
- [ ] 3.2 Update Twilio webhook: log to S3 → respond to Twilio → store to SQLite → publish to Adafruit IO
- [ ] 3.3 Add `GET /api/messages?since={timestamp}` endpoint returning JSON array
- [ ] 3.4 Add `GET /api/config` returning current config JSON
- [ ] 3.5 Add `PUT /api/config` accepting full config JSON, storing to SQLite, saving S3 snapshot, publishing to Adafruit IO
- [ ] 3.6 Add `POST /api/messages/{id}/suppress` adding `type=message` filter rule
- [ ] 3.7 Add `POST /api/messages/{id}/unsuppress` removing `type=message` filter rule

## 4. Admin UI

- [ ] 4.1 Create `heart-sms-receiver/templates/base.html` with Bootstrap layout
- [ ] 4.2 Create `GET /` (Dashboard) showing recent messages and counts
- [ ] 4.3 Create `GET /messages` with pagination (50 per page), suppress/unsuppress buttons
- [ ] 4.4 Create `GET/POST /filters` listing, adding, and deleting filter rules
- [ ] 4.5 Create `GET/POST /settings` for allowed_senders, rendering, sign name
- [ ] 4.6 Create `GET /preview` with toggle for include_filtered, showing filter reason for suppressed messages
- [ ] 4.7 Wire settings/filters/message actions to save config to SQLite, save S3 snapshot, publish to Adafruit IO

## 5. Config Publish to Adafruit IO

- [ ] 5.1 Create `lib/publish.py` with `publish_config(config)` that sends config JSON to Adafruit IO HTTP
- [ ] 5.2 Call `publish_config()` after any config change via admin UI (settings, filters, suppress/unsuppress)
