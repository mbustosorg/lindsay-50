## Why

The Flask admin UI currently has no authentication. Any person with the URL can send messages, change settings, and view the message log. We need to protect it with a shared credential screen.

## What Changes

- Add a login page (`GET /login`) with username/password fields
- Add a login handler (`POST /login`) that validates against `ADMIN_USERNAME`/`ADMIN_PASSWORD`
- Add session management (Flask-Login)
- Add logout (`/logout`)
- Protect all admin routes behind auth (decorators)
- Add API key auth for ESP32 REST clients via `X-API-Key` header + `API_SECRET_KEY`
- Verify Twilio webhook signatures on incoming requests using `TWILIO_AUTH_TOKEN`

## Capabilities

### New Capabilities
- `user-auth`: Login screen + session-based auth for browser clients. Two credential sets:
  - `ADMIN_USERNAME` + `ADMIN_PASSWORD`: browser login form fields
  - `API_SECRET_KEY`: machine-to-machine auth via `X-API-Key` header (ESP32 REST clients)
- `twilio-webhook-verification`: Verify Twilio's HMAC-SHA1 signature on incoming webhook requests using the `TWILIO_AUTH_TOKEN` config value

### Modified Capabilities
- (none)

## Impact

- New routes: `/login`, `/logout`
- Protected routes: all admin UI and REST endpoints (`/`, `/messages`, `/settings`, `/api/*`, etc.)
- Health check `/health` remains unauthenticated (load-balancer probing)
- New dependency: `Flask-Login` for session management
- Credentials stored in `settings.toml` `[auth]` section:
  - `ADMIN_USERNAME`, `ADMIN_PASSWORD`: browser login
  - `API_SECRET_KEY`: ESP32 API key via `X-API-Key` header
- Twilio webhook handler (`POST /api/messages`) adds signature verification
