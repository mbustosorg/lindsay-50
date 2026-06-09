## Context

The Flask admin UI (`heart-message-manager`) is unauthenticated. Anyone with the URL can send SMS messages, view logs, and change settings. Two client types need access:

- **Browser**: Human admin using the web UI
- **ESP32**: Machine client polling Flask's REST API for config and messages

Credentials are separate for each client type and stored in `settings.toml`.

## Goals / Non-Goals

**Goals:**
- Add a login screen with username/password for browser clients
- Protect all admin routes with session-based auth (Flask-Login)
- Support ESP32 API access via `X-API-Key` header
- Verify Twilio webhook signatures on incoming webhook requests
- Health check endpoint remains unauthenticated
- Sessions expire after `ADMIN_SESSION_TIMEOUT_MINS` minutes of inactivity (sliding window)

**Non-Goals:**
- Per-user accounts or user management (single shared credential per client type)
- OAuth, SSO, or social login
- Rate limiting
- Outbound request signing (inbound Twilio verification only)
- Modifying ESP32 code (ESP32 auth implementation is out of scope here)

## Decisions

### Auth library: Flask-Login

Using `Flask-Login` for browser session management. It handles cookies, session lifetime, and route protection cleanly.

**Alternatives considered:**
- `flask.session` directly: too barebones; would reimplement login/logout manually
- `flask-httpauth`: more for API token auth, not session-based UI

### Sliding session expiration

Sessions expire after `ADMIN_SESSION_TIMEOUT_MINS` (default 60) minutes of inactivity, not from login time. Flask-Login's built-in `REMEMBER_COOKIE_DURATION` is fixed, so we implement sliding expiration manually:

- Store `session["_last_activity"]` as a Unix timestamp on every authenticated request
- Before request: if `now - last_activity > ADMIN_SESSION_TIMEOUT_MINS`, treat as unauthenticated and clear session
- On every authenticated request: update `last_activity`

This gives the UX of "active sessions stay open" while still forcing re-login after inactivity.

### Credential storage

`settings.toml` `[auth]` section:
```toml
[auth]
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "your-browser-password"
API_SECRET_KEY = "your-esp32-api-key"
ADMIN_SESSION_TIMEOUT_MINS = 60
```

`TWILIO_AUTH_TOKEN` is added to the root of `settings.toml` (not under `[auth]`) as it is a separate Twilio credential.

### Route protection

All routes require auth except:
- `/health` — load-balancer health checks must stay open
- `/login` — unauthenticated access to login page
- Twilio webhook `/api/messages` (POST) — verified by signature instead

### ESP32 API key authentication

ESP32 sends `X-API-Key: <API_SECRET_KEY>` header on all REST calls. A before-request handler checks this header for non-browser clients (ESP32 doesn't maintain cookies).

Implementation: a before_request function that checks `request.headers.get("X-API-Key")` against `API_SECRET_KEY`. Falls through to Flask-Login session check for browser clients.

### Twilio webhook signature verification

Twilio signs its outgoing webhook requests using HMAC-SHA1. We verify using `twilio.validate_request()` with `TWILIO_AUTH_TOKEN` from config. Applied to `POST /api/messages` only.

## Risks / Trade-offs

[Risk] Shared credential means anyone with the password has full browser access → **Mitigation**: Use a strong password; rotate via config file edit
[Risk] Session hijacking if cookie not HTTPS → **Mitigation**: Set `SESSION_COOKIE_SECURE = True` in production; HttpOnly cookies via Flask-Login defaults
[Risk] ESP32 API key visible in transit → **Mitigation**: ESP32 should use HTTPS in production; key is static and long
[Risk] ESP32 has full API access (no per-device auth) → **Mitigation**: Separate from browser credentials; scoped to ESP32's needs only

## Migration Plan

1. Add `Flask-Login` to `requirements.txt`
2. Add `[auth]` section to `settings.toml.example` (ADMIN_USERNAME, ADMIN_PASSWORD, API_SECRET_KEY, ADMIN_SESSION_TIMEOUT_MINS)
3. Add `TWILIO_AUTH_TOKEN` to `settings.toml.example`
4. Create `heart-message-manager/auth.py` with:
   - `auth_bp`: login/logout routes
   - Sliding session expiration helper
   - Before-request handler checking `X-API-Key` header
5. Update `main.py` to init Flask-Login, register blueprint, protect routes
6. Add Twilio signature verification to `POST /api/messages`
7. Test: login flow, protected routes redirect to login, ESP32 API calls with wrong key return 401, Twilio signature rejection, inactivity timeout
8. Deploy — no DB migration; credentials in config
