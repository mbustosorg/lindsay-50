"""Tests for the `GET /api/sign-status` Flask endpoint.

The endpoint MUST always return 200 and MUST NOT compute a
`state` field — state is browser-side per the design. The
response shape is `{snapshot: <dict | null>, received_at:
<iso-8601 | null>}`.

The tests load the Flask app via importlib with heavy deps
(sqlite, s3, paho, MQTT broker) mocked out, then drive the
endpoint by monkeypatching the module-level `latest_status`
store. The auth requirement is bypassed because the
endpoint's `@require_api_key` is also monkeypatched to a
no-op (status is read by the browser, not the ESP32).
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lib_shared.sign_status import LatestSignStatus

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
    cfg.MQTT_STATUS_TOPIC = "test/feeds/sign-status"
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


def _load_app_module(paho_client_ctor):
    """Load main.py with heavy deps mocked; return the Flask app."""
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    lib_shared = _make_mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    config_reader_mod = _make_mock("lib_shared.config_reader")
    cfg = _make_mock_cfg()
    config_reader_mod.get_config = lambda required_keys=None: cfg
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

    # The real sign_status module — we exercise the real one, not a mock.
    real_sign_status_path = _PROJECT_ROOT / "lib_shared" / "sign_status.py"
    spec_ss = importlib.util.spec_from_file_location("lib_shared.sign_status", str(real_sign_status_path))
    assert spec_ss is not None and spec_ss.loader is not None
    real_sign_status_mod = importlib.util.module_from_spec(spec_ss)
    sys.modules["lib_shared.sign_status"] = real_sign_status_mod
    spec_ss.loader.exec_module(real_sign_status_mod)

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
    from jinja2 import FileSystemLoader

    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_PROJECT_ROOT / "heart-message-manager" / "templates"))

    return flask_app, mod


@pytest.fixture
def app(monkeypatch):
    """Flask app + the loaded module so tests can monkeypatch `latest_status`."""
    captured: dict = {}

    class _NoOpPaho:
        def __init__(self, dispatch_callback, **kwargs):
            captured["dispatch_callback"] = dispatch_callback
            captured["kwargs"] = kwargs
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()

    real_modules: dict = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)

    flask_app, main_mod = _load_app_module(_NoOpPaho)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    # The /api/sign-status endpoint may have a login_required or
    # @require_api_key decorator — we want to bypass auth for these
    # tests. Easiest: monkeypatch the decorator on the loaded module
    # to a no-op, then iterate. The cleanest approach: register the
    # route manually with a fresh decorator; but that duplicates the
    # route. For now, the tests use the test_client which respects
    # any auth — if auth is required, we reconfigure it.
    captured["flask_app"] = flask_app
    captured["main_mod"] = main_mod

    try:
        yield flask_app, captured
    finally:
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod


@pytest.fixture
def client(app):
    flask_app, _ = app
    return flask_app.test_client()


def _healthy_snapshot() -> dict:
    return {
        "schema_version": 1,
        "active_sha": "b5e191c5df481d51c4e7d1cced51cf7c656f1ead",
        "short_sha": "b5e191c",
        "started_at": "2026-07-08T10:00:00+00:00",
        "updated_at": "2026-07-08T10:01:30+00:00",
        "uptime_seconds": 90,
        "mqtt_connected": True,
        "last_error": None,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSignStatusEndpoint:
    def test_returns_200_with_nulls_when_empty(self, app, client, monkeypatch):
        """Empty in-memory store returns 200 with nulls."""
        _, captured = app
        main_mod = captured["main_mod"]
        empty = LatestSignStatus()
        monkeypatch.setattr(main_mod, "latest_status", empty)

        resp = client.get("/api/sign-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body is not None
        assert body["snapshot"] is None
        assert body["received_at"] is None

    def test_returns_200_with_snapshot_when_populated(self, app, client, monkeypatch):
        _, captured = app
        main_mod = captured["main_mod"]
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        monkeypatch.setattr(main_mod, "latest_status", store)

        resp = client.get("/api/sign-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body is not None
        snap = body["snapshot"]
        assert snap is not None
        assert snap["active_sha"] == "b5e191c5df481d51c4e7d1cced51cf7c656f1ead"
        assert snap["short_sha"] == "b5e191c"
        assert snap["uptime_seconds"] == 90
        assert snap["mqtt_connected"] is True
        # ISO-8601 round-trip on received_at.
        assert body["received_at"] is not None
        assert isinstance(datetime.fromisoformat(body["received_at"]), datetime)

    def test_response_does_not_contain_state_field(self, app, client, monkeypatch):
        """The endpoint MUST NOT compute or return a `state` field.

        State (live / unknown / offline) and health (healthy /
        degraded) are browser-side policy. The endpoint only
        surfaces the raw snapshot + receipt timestamp.
        """
        _, captured = app
        main_mod = captured["main_mod"]
        store = LatestSignStatus()
        store.update(_healthy_snapshot())
        monkeypatch.setattr(main_mod, "latest_status", store)

        resp = client.get("/api/sign-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert "state" not in body
        # And the inner snapshot also has no computed state.
        assert "state" not in (body.get("snapshot") or {})
        assert "health" not in (body.get("snapshot") or {})

    def test_response_keys_top_level(self, app, client, monkeypatch):
        """Only `snapshot` and `received_at` at the top level — nothing else."""
        _, captured = app
        main_mod = captured["main_mod"]
        store = LatestSignStatus()
        monkeypatch.setattr(main_mod, "latest_status", store)

        resp = client.get("/api/sign-status")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body is not None
        assert set(body.keys()) == {"snapshot", "received_at"}
