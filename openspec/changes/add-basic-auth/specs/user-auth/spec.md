## ADDED Requirements

### Requirement: Browser login with username and password

The system SHALL present a login page at `GET /login` with username and password fields. Submitting `POST /login` with valid `ADMIN_USERNAME` and `ADMIN_PASSWORD` from config SHALL establish an authenticated session. Invalid credentials SHALL return a flash error and re-render the login page.

#### Scenario: Successful login redirects to dashboard

- **WHEN** user submits `POST /login` with correct `ADMIN_USERNAME` and `ADMIN_PASSWORD`
- **THEN** the system establishes an authenticated session and redirects to `/`

#### Scenario: Failed login shows error

- **WHEN** user submits `POST /login` with incorrect password
- **THEN** the system returns a flash error message and re-renders the login page with a 200 status

#### Scenario: Unauthenticated browser access redirects to login

- **WHEN** an unauthenticated browser requests any protected route (e.g., `GET /messages`)
- **THEN** the system redirects to `GET /login?next=<original path>`

### Requirement: Session management with inactivity timeout

The system SHALL track the last activity timestamp in the session on every authenticated request. If more than `ADMIN_SESSION_TIMEOUT_MINS` minutes have elapsed since the last activity, the session SHALL be treated as expired and the user redirected to login.

#### Scenario: Session expires after inactivity

- **WHEN** an authenticated user's last activity was more than `ADMIN_SESSION_TIMEOUT_MINS` minutes ago and they request a protected page
- **THEN** the system clears the session and redirects to `GET /login`

#### Scenario: Session stays active with regular use

- **WHEN** an authenticated user makes a request within `ADMIN_SESSION_TIMEOUT_MINS` of their last activity
- **THEN** the system updates the last activity timestamp and serves the requested page

### Requirement: Logout destroys session

`GET /logout` SHALL clear the session and redirect to `GET /login`. A subsequent request to a protected route without re-authentication SHALL be rejected.

#### Scenario: Logout clears session

- **WHEN** authenticated user visits `GET /logout`
- **THEN** the session is cleared and the user is redirected to `GET /login`

### Requirement: ESP32 API key authentication

The system SHALL accept `X-API-Key: <API_SECRET_KEY>` header on REST API endpoints. When this header is present and valid, the request SHALL be treated as authenticated without requiring a session cookie. When the header is absent or incorrect, the system SHALL return HTTP 401.

#### Scenario: Valid API key grants access

- **WHEN** ESP32 sends a request to a protected endpoint (e.g., `GET /api/live-messages`) with header `X-API-Key: <API_SECRET_KEY>`
- **THEN** the system returns 200 with the expected JSON response

#### Scenario: Missing API key returns 401

- **WHEN** a request to a protected endpoint has no `X-API-Key` header
- **THEN** the system returns HTTP 401 with a JSON error body `{"error": "missing API key"}`

#### Scenario: Invalid API key returns 401

- **WHEN** a request to a protected endpoint has an incorrect `X-API-Key` value
- **THEN** the system returns HTTP 401 with a JSON error body `{"error": "invalid API key"}`

### Requirement: Health endpoint is unauthenticated

`GET /health` SHALL always return HTTP 200 with `"ok"` regardless of authentication state.

#### Scenario: Health check always returns 200

- **WHEN** any client requests `GET /health`
- **THEN** the system returns HTTP 200 with body `"ok"`
