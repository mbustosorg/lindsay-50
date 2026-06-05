## 1. Setup

- [x] 1.1 Add Flask-Login to requirements.txt
- [x] 1.2 Add `[auth]` section to settings.toml.example (ADMIN_USERNAME, ADMIN_PASSWORD, API_SECRET_KEY, ADMIN_SESSION_TIMEOUT_MINS)
- [x] 1.3 Add TWILIO_AUTH_TOKEN to settings.toml.example

## 2. Auth module

- [x] 2.1 Create heart-message-manager/auth.py with LoginManager, User class, login/logout routes, sliding session logic, and API key before_request handler
- [x] 2.2 Create templates/login.html with username/password form

## 3. Integrate auth into main.py

- [x] 3.1 Init Flask-Login in main.py and register auth blueprint
- [x] 3.2 Protect all admin routes with @login_required (except /health, /login, /logout)
- [x] 3.3 Add Twilio signature verification to POST /api/messages using twilio.validate_request()
- [x] 3.4 Ensure ESP32 REST endpoints accept X-API-Key header auth

## 4. Update existing templates for auth

- [x] 4.1 Update base template to show logout link when authenticated
- [x] 4.2 Redirect to /login when unauthenticated user hits any protected route

## 5. Tests

- [x] 5.1 Test login success — valid ADMIN_USERNAME/ADMIN_PASSWORD returns session and redirects to /
- [x] 5.2 Test login failure — invalid credentials shows error and re-renders login page
- [x] 5.3 Test session inactivity timeout — request after ADMIN_SESSION_TIMEOUT_MINS clears session
- [x] 5.4 Test logout — GET /logout clears session and redirects to /login
- [x] 5.5 Test API key auth — valid X-API-Key header grants access to protected endpoint
- [x] 5.6 Test API key missing — request without X-API-Key returns 401
- [x] 5.7 Test API key invalid — wrong X-API-Key value returns 401
- [x] 5.8 Test health endpoint — GET /health returns 200 without auth
- [x] 5.9 Test Twilio signature valid — valid signature on POST /api/messages processes webhook
- [x] 5.10 Test Twilio signature invalid — missing/invalid signature on POST /api/messages returns 403
