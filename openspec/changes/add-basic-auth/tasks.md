## 1. Setup

- [ ] 1.1 Add Flask-Login to requirements.txt
- [ ] 1.2 Add `[auth]` section to settings.toml.example (ADMIN_USERNAME, ADMIN_PASSWORD, API_SECRET_KEY, ADMIN_SESSION_TIMEOUT_MINS)
- [ ] 1.3 Add TWILIO_AUTH_TOKEN to settings.toml.example

## 2. Auth module

- [ ] 2.1 Create heart-message-manager/auth.py with LoginManager, User class, login/logout routes, sliding session logic, and API key before_request handler
- [ ] 2.2 Create templates/login.html with username/password form

## 3. Integrate auth into main.py

- [ ] 3.1 Init Flask-Login in main.py and register auth blueprint
- [ ] 3.2 Protect all admin routes with @login_required (except /health, /login, /logout)
- [ ] 3.3 Add Twilio signature verification to POST /api/messages using twilio.validate_request()
- [ ] 3.4 Ensure ESP32 REST endpoints accept X-API-Key header auth

## 4. Update existing templates for auth

- [ ] 4.1 Update base template to show logout link when authenticated
- [ ] 4.2 Redirect to /login when unauthenticated user hits any protected route

## 5. Tests

- [ ] 5.1 Test login success — valid ADMIN_USERNAME/ADMIN_PASSWORD returns session and redirects to /
- [ ] 5.2 Test login failure — invalid credentials shows error and re-renders login page
- [ ] 5.3 Test session inactivity timeout — request after ADMIN_SESSION_TIMEOUT_MINS clears session
- [ ] 5.4 Test logout — GET /logout clears session and redirects to /login
- [ ] 5.5 Test API key auth — valid X-API-Key header grants access to protected endpoint
- [ ] 5.6 Test API key missing — request without X-API-Key returns 401
- [ ] 5.7 Test API key invalid — wrong X-API-Key value returns 401
- [ ] 5.8 Test health endpoint — GET /health returns 200 without auth
- [ ] 5.9 Test Twilio signature valid — valid signature on POST /api/messages processes webhook
- [ ] 5.10 Test Twilio signature invalid — missing/invalid signature on POST /api/messages returns 403
