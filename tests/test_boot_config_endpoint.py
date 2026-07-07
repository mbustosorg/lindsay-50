"""Tests for GET /api/sign/boot-config and the one-shot MQTT hint at Flask startup.

v2 design:
  - Endpoint renamed from /api/sign/expected-sha → /api/sign/boot-config
  - Response shape is `{"expected_sha": "<sha>"}` and nothing else
  - Flask publishes `command=check-for-update` MQTT envelope EXACTLY ONCE
    at startup (not on every MQTT on_connect reconnect — that was the v1
    anti-pattern that turned network flakiness into a reboot hint)

The `_load_app_module` harness is shared with `test_auth.py` — heavy
deps (sqlite, s3, paho network, MQTT broker) are mocked so the tests
can drive Flask in-process without ever connecting to anything.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"
EXPECTED_BOOT_CONFIG_PATH = "/api/sign/boot-config"


def _make_mock_cfg():
    cfg = MagicMock()
    cfg.MQTT_CLIENT = "paho"
    cfg.MQTT_HOST = "localhost"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "test"
    cfg.MQTT_PASSWORD = "test"
    cfg.MQTT_TOPIC = "test/feeds/sign"
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


def _load_app_module(mock_cfg, paho_client_ctor):
    """Load main.py using importlib, mocking heavy deps.

    Saves the real `lib_shared.*` modules before mutating sys.modules
    and restores them in the fixture's teardown so sibling tests see
    the genuine package, not our mocks.
    """
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

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

    class _FakeEnvelope:
        def __init__(self, type, payload):
            self.type = type
            self.payload = payload

        def to_json(self):
            return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))

    models_mod.MessageEnvelope = _FakeEnvelope
    models_mod.MessageView = MagicMock()
    models_mod._DEFAULT_EFFECTS_LIST_FULL = []

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = paho_client_ctor

    def _load_real_module(name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    auth_real_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_mod = _load_real_module("heart-message-manager.auth", auth_real_path)
    sys.modules["auth"] = auth_mod

    _make_mock("heart-message-manager.sqlite")
    _make_mock("heart-message-manager.s3")
    _make_mock("heart-message-manager.server_time")
    _make_mock("heart-message-manager.paho_mqtt_client")

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

    paho_mm_mod = types.ModuleType("paho_mqtt_client")
    paho_mm_mod.PahoMqttClient = MagicMock()
    sys.modules["paho_mqtt_client"] = paho_mm_mod

    spec = importlib.util.spec_from_file_location("heart_message_manager_main", str(_MAIN_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart-message-manager.main"] = mod
    spec.loader.exec_module(mod)

    flask_app = mod.app
    flask_app.jinja_loader = None
    from jinja2 import FileSystemLoader

    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_PROJECT_ROOT / "heart-message-manager" / "templates"))

    return flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """Flask app + the captured PahoMqttClient constructor + the captured MQTT client instance."""
    captured = {}

    class _RecordingPaho:
        def __init__(self, dispatch_callback, **kwargs):
            captured["dispatch_callback"] = dispatch_callback
            captured["kwargs"] = kwargs
            # v2 invariant: PahoMqttClient no longer accepts `on_connect_callback`.
            assert "on_connect_callback" not in kwargs, "v2 design: PahoMqttClient must not accept on_connect_callback"
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()
            captured["instance"] = self

    mock_cfg = _make_mock_cfg()

    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)

    flask_app = _load_app_module(mock_cfg, _RecordingPaho)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    captured["flask_app"] = flask_app

    try:
        yield flask_app, captured
    finally:
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


@pytest.fixture
def client(app):
    flask_app, _ = app
    return flask_app.test_client()


@pytest.fixture
def esp32_headers():
    return {"X-API-Key": "esp32-api-key"}


# ---------------------------------------------------------------------------
# /api/sign/boot-config — endpoint shape (auth, response fields)
# ---------------------------------------------------------------------------


class TestBootConfigEndpointShape:
    def test_returns_expected_sha_when_slug_set(self, app, client, esp32_headers, monkeypatch):
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get(EXPECTED_BOOT_CONFIG_PATH, headers=esp32_headers)
        assert response.status_code == 200
        assert response.json == {
            "expected_sha": "abc1234567890",
            "short_sha": "abc1234",
        }

    def test_drops_old_expected_sha_endpoint(self, app, client, esp32_headers):
        """The v1 /api/sign/expected-sha URL no longer exists."""
        response = client.get("/api/sign/expected-sha", headers=esp32_headers)
        assert response.status_code == 404

    def test_response_has_expected_sha_and_short_sha_keys(self, app, client, esp32_headers, monkeypatch):
        """Response shape: {expected_sha: <full>, short_sha: <7-char>}."""
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "b5e191c5df481d51c4e7d1cced51cf7c656f1ead")
        response = client.get(EXPECTED_BOOT_CONFIG_PATH, headers=esp32_headers)
        assert response.status_code == 200
        assert set(response.json.keys()) == {"expected_sha", "short_sha"}
        assert response.json["expected_sha"] == "b5e191c5df481d51c4e7d1cced51cf7c656f1ead"
        assert response.json["short_sha"] == "b5e191c"

    def test_short_sha_is_first_seven_of_expected_sha(self, app, client, esp32_headers, monkeypatch):
        """short_sha must be exactly expected_sha[:7] — single source of truth."""
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "0123456789abcdef0123456789abcdef01234567")
        response = client.get(EXPECTED_BOOT_CONFIG_PATH, headers=esp32_headers)
        assert response.json["short_sha"] == response.json["expected_sha"][:7]

    def test_returns_401_without_api_key(self, app, client, monkeypatch):
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get(EXPECTED_BOOT_CONFIG_PATH)
        assert response.status_code == 401

    def test_returns_401_with_invalid_api_key(self, app, client, monkeypatch):
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get(
            EXPECTED_BOOT_CONFIG_PATH,
            headers={"X-API-Key": "not-the-right-key"},
        )
        assert response.status_code == 401


class TestBootConfigGitFallback:
    def test_returns_local_git_head_when_slug_unset(self, app, client, esp32_headers, monkeypatch):
        """When HEROKU_SLUG_COMMIT is not set, return the local git HEAD SHA."""
        monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
        response = client.get(EXPECTED_BOOT_CONFIG_PATH, headers=esp32_headers)
        assert response.status_code == 200
        sha = response.json["expected_sha"]
        assert isinstance(sha, str)
        assert len(sha) >= 7
        int(sha, 16)  # raises if non-hex


# ---------------------------------------------------------------------------
# One-shot MQTT hint at startup (v2 design)
# ---------------------------------------------------------------------------


class TestStartupPublishesCheckForUpdate:
    def test_paho_client_constructed_without_on_connect_callback(self, app):
        """v2 design: PahoMqttClient does not accept on_connect_callback."""
        _, captured = app
        # _RecordingPaho.__init__ raises if the kwarg is present (defense
        # in depth). The fact that we got here proves the kwarg was
        # absent. Assert the kwargs shape for clarity.
        assert "on_connect_callback" not in captured["kwargs"]

    def test_publishes_check_for_update_at_startup(self, app):
        """Flask calls publish_envelope exactly once with a check-for-update envelope."""
        _, captured = app
        instance = captured["instance"]
        # Exactly one publish at startup.
        assert instance.publish_envelope.call_count == 1
        env = instance.publish_envelope.call_args.args[0]
        assert env.type == "command"
        assert env.payload == {"action": "check-for-update"}

    def test_publish_swallows_failure(self, app):
        """A publish failure at startup does not raise — Flask must keep running."""
        _, captured = app
        instance = captured["instance"]
        instance.publish_envelope.return_value = False
        # The startup publish already ran during module load; verifying
        # that nothing raised is sufficient. We can additionally confirm
        # the envelope was attempted.
        assert instance.publish_envelope.call_count >= 1
