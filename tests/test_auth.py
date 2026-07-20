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

    # Mock lib_shared submodules. The parent `lib_shared` mock gets a
    # real `__path__` so Python's import system can resolve any
    # submodules we DON'T mock (e.g. `lib_shared.scroller_base`) from
    # the real filesystem; without `__path__`, the import falls through
    # with `'lib_shared' is not a package` because a bare
    # `types.ModuleType` carries no package metadata.
    lib_shared = _make_mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
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
    # TextSettings was added to main.py's import line as part of the
    # #6 senders-filtering refactor — when the mock replaces
    # lib_shared.models, main.py's `from lib_shared.models import
    # EffectsSettings, TextSettings` would fail without a stand-in here.
    models_mod.TextSettings = MagicMock()
    # New after the #26 effects-settings redesign — `main.py`'s
    # `_build_sign_config_from_request` validator consults
    # `EffectsSettings.MIN_LOOKBACK_DAYS`, `MAX_LOOKBACK_DAYS`, and
    # `VALID_SELECTOR_ALGORITHMS`, so the mock loader has to expose
    # them with real class-level attributes (a bare MagicMock would
    # accept `MIN_LOOKBACK_DAYS` reads but the value would be a
    # Mock, not a number — validator comparisons would silently lie).
    effects_settings_mock = MagicMock()
    effects_settings_mock.MIN_LOOKBACK_DAYS = 1
    effects_settings_mock.MAX_LOOKBACK_DAYS = 365
    effects_settings_mock.VALID_SELECTOR_ALGORITHMS = ("weighted", "random")
    models_mod.EffectsSettings = effects_settings_mock

    # Mock the v2 config migration module (heart-message-manager/main.py
    # imports it at module level). The startup migration runs at app load
    # time, so the call must be a no-op MagicMock.
    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()
    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = MagicMock()

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
    sqlite_mod.put_config = MagicMock()
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
    # adafruit_mqtt_client is gone; the paho client is mocked above
    # (lib_shared.paho_mqtt_client.PahoMqttClient). The bare `paho_mqtt_client`
    # entry below stays in case any legacy import path remains.
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
        """POST /login with correct ADMIN_USERNAME/ADMIN_PASSWORD redirects to /?wipe=1.

        The login route appends `?wipe=1` so the client-side app wipes
        IndexedDB and re-seeds from REST on this load (see auth.py).
        """
        response = client.post(
            "/login",
            data={"username": "admin", "password": "secret123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.location == "/?wipe=1"


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
        from twilio.request_validator import RequestValidator  # type: ignore[import-untyped]

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

    def test_twilio_webhook_logs_all_fields_including_media_urls(self, app, client, caplog):
        """Every incoming Twilio field — NumMedia, MediaUrl0..N, SmsSid,
        MessageStatus, etc. — must appear in the journalctl output so
        the operator can debug MMS webhooks, status callbacks, and any
        future Twilio fields without re-deploying.

        The pretty-printed log uses ``json.dumps(indent=2)`` so each
        field is on its own line; the test asserts the field NAMES are
        present (and the values are reachable via substring match), not
        the exact indent, so it doesn't break on dict-ordering changes.
        """
        import logging
        from twilio.request_validator import RequestValidator

        url = "https://lindsay-50.herokuapp.com/api/messages"
        # A realistic MMS webhook payload: From/Body + NumMedia=1 +
        # MediaUrl0 + the matching content-type. Twilio's actual webhook
        # also includes SmsSid, AccountSid, ApiVersion, MessageSid,
        # MessagingServiceSid, etc.; we test the ones the user asked
        # about (MediaUrl) plus the standard SMS fields.
        params = {
            "From": "+15551234567",
            "To": "+15559999999",
            "Body": "photo of my dog",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/2010-04-01/Accounts/AC.../Messages/MM.../Media/ME...",
            "MediaContentType0": "image/jpeg",
            "SmsSid": "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "SmsStatus": "received",
            "AccountSid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            "ApiVersion": "2010-04-01",
        }
        validator = RequestValidator("twilio-auth-token")
        signature = validator.compute_signature(url, params)

        with caplog.at_level(logging.INFO):
            response = client.post(
                "/api/messages",
                data=params,
                headers={
                    "X-Twilio-Signature": signature,
                    "Host": "lindsay-50.herokuapp.com",
                    "X-Forwarded-Proto": "https",
                },
            )

        assert response.status_code in (200, 204)
        # The pretty-printed field log must contain every incoming key.
        # We join caplog text once and substring-search — JSON indent
        # rendering shouldn't matter, only that the keys + values are
        # both there.
        joined = "\n".join(record.getMessage() for record in caplog.records)
        for key, value in params.items():
            assert f'"{key}"' in joined, f"key {key!r} missing from webhook log"
            # The string-form of the value must also be present (json
            # dumps with default=str will render MediaUrl0 as-is).
            assert str(value) in joined, f"value {value!r} for key {key!r} missing from webhook log"

        # Sanity: the validation summary line is also logged (different
        # message but same level), so the count assertion is on
        # "at least one pretty-printed record + one summary record".
        pretty_records = [r for r in caplog.records if "Twilio webhook fields" in r.getMessage()]
        assert len(pretty_records) >= 1, "expected at least one pretty-printed fields log line"


# ---------------------------------------------------------------------------
# 5.11 — /settings POST: form field-name merge contract
# ---------------------------------------------------------------------------


class TestSettingsSaveFormFieldMerge:
    """`templates/settings.html` posts pacing fields as
    `effects_settings_fade_seconds`, `effects_settings_hold_seconds`,
    etc. (underscore separators). The Python handler MUST look those
    exact field names up — if it concatenates ``f"effects_settings{field}"``
    it builds ``effects_settingsfade_seconds`` (no separator) and the
    ``request.form.get(...)`` returns ``None`` for every pacing field,
    so ``setattr`` never runs and the user's value silently doesn't
    save. These tests pin the contract — both that the right name is
    queried AND that the raw POST is logged for future debugging.
    """

    def _baseline_cfg(self):
        """A MagicMock that mimics SignConfig well enough for the merge.

        The /settings handler reads `.sign.name`, `.timezone`,
        `.text_settings.<attr>`, `.effects_settings.<attr>`, `.filters`,
        `.senders`. MagicMock returns a child mock for every attribute
        access, so the read paths work; the setattr calls also work
        because MagicMock records attribute writes and returns the
        last-written value on subsequent reads — which is what we use
        to verify the merge landed.
        """
        cfg = MagicMock()
        cfg.sign.name = "Lindsay's Heart"
        cfg.timezone = "America/Los_Angeles"
        cfg.filters = []
        cfg.senders = {}
        return cfg

    def test_post_settings_saves_fade_seconds_from_form(self, app, client):
        """If the user POSTs effects_settings_fade_seconds=5, the saved
        config's effects_settings.fade_seconds must equal 5.0.
        Regression for the field-name mismatch that previously made
        every pacing save silently revert to the loaded baseline."""
        import sqlite as sqlite_mod

        baseline_cfg = self._baseline_cfg()
        sqlite_mod.get_config.return_value = baseline_cfg

        # Login via the standard path so the session is authenticated.
        client.post("/login", data={"username": "admin", "password": "secret123"})

        response = client.post(
            "/settings",
            data={
                "sign_name": "Lindsay's Heart",
                "timezone": "America/Los_Angeles",
                "effects_settings_fade_seconds": "5",
                "effects_settings_hold_seconds": "20",
                "effects_settings_intro_seconds": "5",
                "effects_settings_idle_seconds": "300",
                "effects_settings_lookback_days": "21",
                "effects_settings_selector_algorithm": "weighted",
            },
            follow_redirects=False,
        )

        # Successful save → 302 redirect to /settings
        assert response.status_code == 302, response.data

        # All four pacing fields, `lookback_days`, and `selector_algorithm`
        # must have landed on the cfg that was passed to put_config
        # (MagicMock attribute writes are recordable, so we read them
        # back through the cfg MagicMock).
        assert baseline_cfg.effects_settings.fade_seconds == 5.0
        assert baseline_cfg.effects_settings.hold_seconds == 20.0
        assert baseline_cfg.effects_settings.intro_seconds == 5.0
        assert baseline_cfg.effects_settings.idle_seconds == 300.0
        assert baseline_cfg.effects_settings.lookback_days == 21
        assert baseline_cfg.effects_settings.selector_algorithm == "weighted"

        # And the saved cfg must have been persisted to sqlite.
        assert sqlite_mod.put_config.called, "expected sqlite.put_config to be called"

    def test_post_settings_leaves_pacing_alone_when_field_absent(self, app, client):
        """If the form omits a pacing field (e.g. a /settings save that
        only filters), the existing value must survive. This pins the
        guard that `if raw is not None and raw != "":` provides — without
        that guard the field would silently reset to 0.0 on every save."""
        import sqlite as sqlite_mod

        baseline_cfg = self._baseline_cfg()
        # Set non-zero baselines so we can detect an accidental zero.
        baseline_cfg.effects_settings.fade_seconds = 2.0
        baseline_cfg.effects_settings.hold_seconds = 15.0
        sqlite_mod.get_config.return_value = baseline_cfg

        client.post("/login", data={"username": "admin", "password": "secret123"})

        # POST with NONE of the pacing fields (e.g. a filter-only save).
        response = client.post(
            "/settings",
            data={
                "sign_name": "Lindsay's Heart",
                "timezone": "America/Los_Angeles",
            },
            follow_redirects=False,
        )

        assert response.status_code == 302, response.data
        # Baselines must still be intact (not overwritten with 0 or None).
        assert baseline_cfg.effects_settings.fade_seconds == 2.0
        assert baseline_cfg.effects_settings.hold_seconds == 15.0

    def test_post_settings_logs_raw_form_and_merge_summary(self, app, client, caplog):
        """The /settings handler MUST log the raw POST fields and a
        per-field pacing merge summary. The raw-form dump is at DEBUG
        (it's noisy at INFO); the merge summary stays at INFO. Without
        the debug dump, the next time a field-name bug shows up, there's
        no on-the-wire evidence of what the form actually submitted —
        flip the caplog level here if you need to inspect it."""
        import logging
        import sqlite as sqlite_mod

        baseline_cfg = self._baseline_cfg()
        sqlite_mod.get_config.return_value = baseline_cfg

        client.post("/login", data={"username": "admin", "password": "secret123"})
        # DEBUG so the raw-form log line (at logger.debug) is captured.
        caplog.set_level(logging.DEBUG)

        response = client.post(
            "/settings",
            data={
                "sign_name": "Lindsay's Heart",
                "timezone": "America/Los_Angeles",
                "effects_settings_fade_seconds": "5",
                "effects_settings_hold_seconds": "15",
                "effects_settings_intro_seconds": "5",
                "effects_settings_idle_seconds": "300",
                "effects_settings_lookback_days": "14",
                "effects_settings_selector_algorithm": "weighted",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302, response.data

        joined = "\n".join(record.getMessage() for record in caplog.records)

        # Raw-form log line must be present and contain every form key.
        raw_records = [r for r in caplog.records if "[settings] POST /settings raw_form_keys=" in r.getMessage()]
        assert len(raw_records) >= 1, "expected a raw-form log line"
        for key in (
            "sign_name",
            "effects_settings_fade_seconds",
            "effects_settings_lookback_days",
        ):
            assert (
                key in raw_records[0].getMessage()
            ), f"raw-form log missing {key!r}; got: {raw_records[0].getMessage()[:500]}"

        # Pacing merge summary must report fade_seconds as POST='5' saved=5.0.
        merge_records = [r for r in caplog.records if "[settings] effect pacing merge" in r.getMessage()]
        assert len(merge_records) >= 1, "expected a pacing-merge log line"
        merge_msg = merge_records[0].getMessage()
        assert "fade_seconds" in merge_msg
        assert "POST='5'" in merge_msg, merge_msg
        assert "saved=5.0" in merge_msg, merge_msg
        assert "lookback_days" in merge_msg
        assert "selector_algorithm" in merge_msg
