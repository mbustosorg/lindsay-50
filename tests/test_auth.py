"""Tests for auth (user auth + Twilio webhook verification)."""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# App fixture — load main.py with all dependencies mocked
# ---------------------------------------------------------------------------

# Absolute path to the project root and main module
_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"


def _make_mock_cfg():
    """Return a mock config object with auth credentials."""
    cfg = MagicMock()
    cfg.MQTT_CLIENT = "paho"
    cfg.MQTT_HOST = "localhost"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "test"
    cfg.MQTT_PASSWORD = "test"
    cfg.MQTT_TOPIC = "test"
    cfg.AWS_ACCESS_KEY_ID = "test"
    cfg.AWS_SECRET_ACCESS_KEY = "test"
    cfg.AWS_S3_BUCKET = "test"
    cfg.AWS_S3_REGION = "us-east-1"
    cfg.CONFIG_API_URL = "http://localhost/api/config"
    cfg.MESSAGES_API_URL = "http://localhost/api/messages"
    cfg.if_exists = MagicMock(
        side_effect=lambda k: {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "secret123",
            "API_SECRET_KEY": "esp32-api-key",
            "ADMIN_SESSION_TIMEOUT_MINS": "60",
            "TWILIO_AUTH_TOKEN": "twilio-auth-token",
        }.get(k)
    )
    return cfg


def _load_app_module(mock_cfg):
    """Load main.py using importlib, mocking all side-effect imports."""
    # Set up mock modules that main.py imports at module level
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    # Mock lib_shared submodules
    lib_shared = _make_mock("lib_shared")
    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg
    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()

    models_mod = _make_mock("lib_shared.models")
    models_mod.SignConfig = MagicMock()
    models_mod.FilterRule = MagicMock()
    models_mod.Message = MagicMock()
    models_mod.MessageEnvelope = MagicMock()
    models_mod.MessageView = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()
    mqtt_factory_mod = _make_mock("lib_shared.mqtt_factory")
    mqtt_factory_mod.make_mqtt_client = MagicMock()

    # Mock heart-message-manager submodules (but load the real auth module)
    # auth.py needs to be imported from the real location
    import importlib.util as _util

    def _load_real_module(name, path):
        spec = _util.spec_from_file_location(name, str(path))
        mod = _util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    auth_real_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_mod = _load_real_module("heart-message-manager.auth", auth_real_path)
    sys.modules["auth"] = auth_mod  # main.py does `import auth`

    _make_mock("heart-message-manager.sqlite")
    _make_mock("heart-message-manager.s3")
    _make_mock("heart-message-manager.server_time")
    _make_mock("heart-message-manager.paho_mqtt_client")

    # Also mock the top-level sqlite, s3 that main.py imports
    sqlite_mod = types.ModuleType("sqlite")
    sqlite_mod.rebuild_from_s3 = MagicMock()
    sqlite_mod.get_config = MagicMock()
    sqlite_mod.get_all_messages = MagicMock(return_value=[])
    sqlite_mod.get_messages_since = MagicMock(return_value=[])
    sqlite_mod.message_count = MagicMock(return_value=0)
    sqlite_mod.put_message = MagicMock()
    sqlite_mod.get_message = MagicMock(return_value=None)
    sys.modules["sqlite"] = sqlite_mod

    s3_mod = types.ModuleType("s3")
    s3_mod.load_messages_from_s3 = MagicMock(return_value=[])
    s3_mod.load_latest_config = MagicMock(return_value=None)
    s3_mod.log_message = MagicMock()
    s3_mod.save_config_snapshot = MagicMock()
    s3_mod._s3_bucket = MagicMock(return_value="test-bucket")
    s3_mod._s3_client = MagicMock()
    sys.modules["s3"] = s3_mod
    server_time_mod = types.ModuleType("server_time")
    server_time_mod.format_from_iso = lambda *args, **kwargs: ""
    server_time_mod.now_utc_iso = lambda: "2026-05-22T00:00:00Z"
    sys.modules["server_time"] = server_time_mod

    # Mock adafruit_mqtt_client (may be imported depending on cfg)
    _make_mock("adafruit_mqtt_client")
    # paho_mqtt_client is imported directly in main.py
    paho_mod = types.ModuleType("paho_mqtt_client")
    paho_mod.PahoMqttClient = MagicMock()
    sys.modules["paho_mqtt_client"] = paho_mod

    # Now load main.py
    import importlib.util

    spec = importlib.util.spec_from_file_location("heart_message_manager_main", str(_MAIN_PATH))
    mod = importlib.util.module_from_spec(spec)

    # Add sys.modules entry so relative imports inside main.py work
    sys.modules["heart-message-manager.main"] = mod

    spec.loader.exec_module(mod)

    flask_app = mod.app
    # Point Jinja to the real templates directory
    flask_app.jinja_loader = None  # reset any cached loader
    from jinja2 import FileSystemLoader

    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_PROJECT_ROOT / "heart-message-manager" / "templates"))

    return flask_app


@pytest.fixture
def app():
    """Create a test Flask app with auth configured and all heavy deps mocked."""
    mock_cfg = _make_mock_cfg()
    # Save the real lib_shared submodules BEFORE _load_app_module replaces
    # them with mocks. _load_app_module installs fake ModuleType objects
    # into sys.modules for "lib_shared", "lib_shared.message_manager",
    # "lib_shared.mqtt_factory", etc. so it can stub out the heavy deps.
    # Other test files (e.g. test_message_manager) need the real
    # lib_shared.message_manager to exist for their own import to work;
    # if we don't restore after the fixture yields, those tests fail
    # with `ModuleNotFoundError: No module named 'lib_shared.messages'`
    # because the cached `lib_shared` parent is now a mock.
    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    flask_app = _load_app_module(mock_cfg)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False
    try:
        yield flask_app
    finally:
        # Restore the real modules so subsequent tests in the same pytest
        # process see the genuine `lib_shared.*` package, not our mocks.
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        # Drop anything that didn't exist before but was added by the
        # mocked main.py load.
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def esp32_headers():
    """X-API-Key header for ESP32 machine clients."""
    return {"X-API-Key": "esp32-api-key"}


# ---------------------------------------------------------------------------
# 5.1 — Login success
# ---------------------------------------------------------------------------


class TestLoginSuccess:
    def test_login_valid_credentials_redirects_to_dashboard(self, client):
        """POST /login with correct ADMIN_USERNAME/ADMIN_PASSWORD redirects to /."""
        response = client.post(
            "/login",
            data={"username": "admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.location == "/"


# ---------------------------------------------------------------------------
# 5.2 — Login failure
# ---------------------------------------------------------------------------


class TestLoginFailure:
    def test_login_invalid_password_shows_error(self, client):
        """POST /login with wrong password re-renders login page with flash error."""
        response = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Invalid username or password" in response.data

    def test_login_wrong_username_shows_error(self, client):
        response = client.post(
            "/login",
            data={"username": "wrong", "password": "secret123"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Invalid username or password" in response.data

    def test_login_empty_credentials_shows_error(self, client):
        response = client.post(
            "/login",
            data={"username": "", "password": ""},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Please enter both username and password" in response.data


# ---------------------------------------------------------------------------
# 5.3 — Session inactivity timeout
# ---------------------------------------------------------------------------


class TestSessionTimeout:
    def test_session_expires_after_timeout(self, app, client):
        """Request after ADMIN_SESSION_TIMEOUT_MINS clears the session."""
        with client.session_transaction() as sess:
            sess["_last_activity"] = time.time() - (61 * 60)
            sess["_timeout_mins"] = 60
            sess["_user_id"] = "admin"

        # Session should be cleared and request redirected to login
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.location

    def test_session_stays_active_within_timeout(self, app, client):
        """Authenticated session within timeout window stays active."""
        # Login first
        client.post("/login", data={"username": "admin", "password": "secret123"})
        # Advance time by only 30 seconds — should still be valid
        with client.session_transaction() as sess:
            sess["_last_activity"] = time.time() - 30
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# 5.4 — Logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_session_and_redirects(self, client):
        """GET /logout clears session and redirects to /login."""
        # Login first
        client.post("/login", data={"username": "admin", "password": "secret123"})
        response = client.get("/logout", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.location


# ---------------------------------------------------------------------------
# 5.5-5.7 — API key auth
# ---------------------------------------------------------------------------


class TestAPIKeyAuth:
    def test_api_key_valid_grants_access(self, app, client, esp32_headers):
        """Valid X-API-Key header grants access to protected API endpoint."""
        response = client.get("/api/messages", headers=esp32_headers)
        assert response.status_code == 200

    def test_api_key_missing_returns_401(self, app, client):
        """Request without X-API-Key returns 401."""
        response = client.get("/api/messages")
        assert response.status_code == 401
        assert response.json == {"error": "missing API key"}

    def test_api_key_invalid_returns_401(self, app, client):
        """Wrong X-API-Key value returns 401."""
        response = client.get("/api/messages", headers={"X-API-Key": "wrong-key"})
        assert response.status_code == 401
        assert response.json == {"error": "missing API key"}


# ---------------------------------------------------------------------------
# 5.8 — Health endpoint
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    def test_health_returns_200_without_auth(self, client):
        """GET /health returns 200 regardless of auth state."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.data == b"ok"


# ---------------------------------------------------------------------------
# 5.9-5.10 — Twilio webhook signature
# ---------------------------------------------------------------------------


class TestTwilioSignature:
    def test_twilio_valid_signature_accepts_webhook(self, app, client):
        """POST /api/messages with valid X-Twilio-Signature processes webhook."""
        from twilio.request_validator import RequestValidator

        # Heroku sets X-Forwarded-Proto: https, so we reconstruct as https
        url = "https://lindsay-50.herokuapp.com/api/messages"
        params = {"From": "+15551234567", "Body": "hello"}
        validator = RequestValidator("twilio-auth-token")
        signature = validator.compute_signature(url, params)

        response = client.post(
            "/api/messages",
            data=params,
            headers={
                "X-Twilio-Signature": signature,
                "Host": "lindsay-50.herokuapp.com",
                "X-Forwarded-Proto": "https",
            },
        )
        # Should return TwiML (200 or 204), not 403
        assert response.status_code in (200, 204)

    def test_twilio_invalid_signature_returns_403(self, app, client):
        """POST /api/messages with invalid signature returns 403."""
        response = client.post(
            "/api/messages",
            data={"From": "+15551234567", "Body": "hello"},
            headers={
                "X-Twilio-Signature": "invalid-signature",
                "Host": "lindsay-50.herokuapp.com",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 403

    def test_twilio_missing_signature_returns_403(self, app, client):
        """POST /api/messages with no X-Twilio-Signature header returns 403."""
        response = client.post(
            "/api/messages",
            data={"From": "+15551234567", "Body": "hello"},
            headers={
                "Host": "lindsay-50.herokuapp.com",
                "X-Forwarded-Proto": "https",
            },
        )
        assert response.status_code == 403
