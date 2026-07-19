"""Tests for POST /api/sign/commands/<action> (issue #51).

The three valid actions (`force-upgrade`, `restart`, `shutdown`) each
publish exactly one `type=command` envelope on the existing
`MQTT_TOPIC`. The endpoint returns 202 on success and 503 when the
broker rejects the publish; an unknown action returns 404.

The `_load_app_module` harness is shared with
`test_boot_config_endpoint.py` / `test_sign_settings_endpoint.py` —
heavy deps (sqlite, s3, paho network, MQTT broker) are mocked so the
tests drive Flask in-process.
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


def _load_app_module(paho_client_ctor, mock_cfg=None):
    if mock_cfg is None:
        mock_cfg = _make_mock_cfg()
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    lib_shared = _make_mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None, _mock_cfg=mock_cfg: _mock_cfg
    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()

    models_mod = _make_mock("lib_shared.models")
    models_mod.SignConfig = MagicMock()
    models_mod.FilterRule = MagicMock()
    models_mod.Message = MagicMock()
    effects_settings_mock = MagicMock()
    effects_settings_mock.MIN_LOOKBACK_DAYS = 1
    effects_settings_mock.MAX_LOOKBACK_DAYS = 365
    effects_settings_mock.VALID_SELECTOR_ALGORITHMS = ("weighted", "random")
    models_mod.EffectsSettings = effects_settings_mock

    class _FakeEnvelope:
        def __init__(self, type, payload):
            self.type = type
            self.payload = payload

        def to_json(self):
            return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))

    models_mod.MessageEnvelope = _FakeEnvelope
    models_mod.MessageView = MagicMock()

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


@pytest.fixture
def app(monkeypatch):
    captured = {}

    class _RecordingPaho:
        def __init__(self, dispatch_callback, **kwargs):
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

    flask_app = _load_app_module(_RecordingPaho)
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
# /api/sign/commands/<action> — auth
# ---------------------------------------------------------------------------


class TestCommandEndpointAuth:
    def test_force_upgrade_requires_api_key(self, app, client):
        response = client.post("/api/sign/commands/force-upgrade")
        assert response.status_code == 401

    def test_restart_requires_api_key(self, app, client):
        response = client.post("/api/sign/commands/restart")
        assert response.status_code == 401

    def test_shutdown_requires_api_key(self, app, client):
        response = client.post("/api/sign/commands/shutdown")
        assert response.status_code == 401

    def test_force_upgrade_rejects_invalid_api_key(self, app, client):
        response = client.post(
            "/api/sign/commands/force-upgrade",
            headers={"X-API-Key": "wrong-key"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/sign/commands/<action> — happy paths
# ---------------------------------------------------------------------------


class TestCommandEndpointSuccess:
    def test_force_upgrade_publishes_command_envelope(self, app, client, esp32_headers):
        _, captured = app
        instance = captured["instance"]
        # The startup check-for-update envelope also went through publish_envelope;
        # the new POST adds one more.
        before = instance.publish_envelope.call_count
        response = client.post("/api/sign/commands/force-upgrade", headers=esp32_headers)
        assert response.status_code == 202
        assert response.json == {"status": "published", "action": "force-upgrade"}

        # Inspect the most recent publish: type=command, payload={action: force-upgrade}.
        calls = instance.publish_envelope.call_args_list[before:]
        assert len(calls) == 1
        env = calls[0].args[0]
        assert env.type == "command"
        assert env.payload == {"action": "force-upgrade"}

    def test_restart_publishes_command_envelope(self, app, client, esp32_headers):
        _, captured = app
        instance = captured["instance"]
        before = instance.publish_envelope.call_count
        response = client.post("/api/sign/commands/restart", headers=esp32_headers)
        assert response.status_code == 202
        env = instance.publish_envelope.call_args_list[before].args[0]
        assert env.type == "command"
        assert env.payload == {"action": "restart"}

    def test_shutdown_publishes_command_envelope(self, app, client, esp32_headers):
        _, captured = app
        instance = captured["instance"]
        before = instance.publish_envelope.call_count
        response = client.post("/api/sign/commands/shutdown", headers=esp32_headers)
        assert response.status_code == 202
        env = instance.publish_envelope.call_args_list[before].args[0]
        assert env.type == "command"
        assert env.payload == {"action": "shutdown"}

    def test_publish_returns_false_503(self, app, client, esp32_headers):
        """publish_envelope returning False → 503 Service Unavailable."""
        _, captured = app
        instance = captured["instance"]
        instance.publish_envelope.return_value = False
        response = client.post("/api/sign/commands/force-upgrade", headers=esp32_headers)
        assert response.status_code == 503
        assert response.json["error"] == "publish failed"

    def test_publish_raises_503(self, app, client, esp32_headers):
        """publish_envelope raising → 503 (Flask keeps running)."""
        _, captured = app
        instance = captured["instance"]
        instance.publish_envelope.side_effect = ConnectionError("broker down")
        response = client.post("/api/sign/commands/force-upgrade", headers=esp32_headers)
        assert response.status_code == 503


# ---------------------------------------------------------------------------
# /api/sign/commands/<action> — unknown actions
# ---------------------------------------------------------------------------


class TestCommandEndpointUnknownAction:
    @pytest.mark.parametrize("action", ["check-for-update", "future-unknown", "bogus", "FORCE-UPGRADE"])
    def test_unknown_action_returns_404(self, app, client, esp32_headers, action):
        response = client.post(f"/api/sign/commands/{action}", headers=esp32_headers)
        assert response.status_code == 404
        assert response.json["action"] == action

    def test_unknown_action_does_not_publish(self, app, client, esp32_headers):
        _, captured = app
        instance = captured["instance"]
        before = instance.publish_envelope.call_count
        client.post("/api/sign/commands/bogus", headers=esp32_headers)
        # No new envelope published for an unknown action.
        assert instance.publish_envelope.call_count == before


# ---------------------------------------------------------------------------
# /api/sign/commands/<action> — only POST is accepted
# ---------------------------------------------------------------------------


class TestCommandEndpointMethodGuard:
    def test_get_force_upgrade_returns_405(self, app, client, esp32_headers):
        response = client.get("/api/sign/commands/force-upgrade", headers=esp32_headers)
        assert response.status_code in (405, 404)


# ---------------------------------------------------------------------------
# Wire-shape sanity: the published envelope matches the spec exactly
# ---------------------------------------------------------------------------


class TestCommandEndpointWireShape:
    @pytest.mark.parametrize("action", ["force-upgrade", "restart", "shutdown"])
    def test_envelope_type_and_payload(self, app, client, esp32_headers, action):
        _, captured = app
        instance = captured["instance"]
        before = instance.publish_envelope.call_count
        client.post(f"/api/sign/commands/{action}", headers=esp32_headers)
        env = instance.publish_envelope.call_args_list[before].args[0]
        # Wire shape per spec: {"type": "command", "payload": {"action": "<name>"}}
        wire = json.loads(env.to_json())
        assert wire == {"type": "command", "payload": {"action": action}}
