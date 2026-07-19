"""Shared in-process Flask harness for the /settings route tests.

Loads ``heart-message-manager/main.py`` with the heavy dependencies
(sqlite, s3, paho network, MQTT broker, message_manager) mocked, so the
``/settings`` GET/POST handlers can be exercised without touching any real
I/O. Modeled on the harness in ``test_admin_settings_route.py`` but factored
out so both the template test and the POST-handler test can reuse it.

This module is intentionally NOT named ``*_test.py`` / ``test_*.py`` so pytest
does not collect it as a test module.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"


def make_mock_cfg():
    """Minimal settings cfg so config_reader and friends have values to read."""
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
    """Load main.py with heavy deps mocked; return the Flask app object."""
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

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = paho_client_ctor

    # phone_utils is real (lightweight, stdlib only) so the handler's
    # normalize_phone import resolves to the true implementation.
    phone_spec = importlib.util.spec_from_file_location(
        "lib_shared.phone_utils", str(_PROJECT_ROOT / "lib_shared" / "phone_utils.py")
    )
    assert phone_spec is not None and phone_spec.loader is not None
    phone_mod = importlib.util.module_from_spec(phone_spec)
    sys.modules["lib_shared.phone_utils"] = phone_mod
    phone_spec.loader.exec_module(phone_mod)

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


class _RecordingPaho:
    """Captures the dispatch callback and records published envelopes."""

    def __init__(self, dispatch_callback, **kwargs):
        self.dispatch_callback = dispatch_callback
        self.kwargs = kwargs
        self.publish_envelope = MagicMock(return_value=True)
        self.start = MagicMock()
        self.stop = MagicMock()


@contextmanager
def settings_app(monkeypatch):
    """Yield ``(flask_app, sqlite_mod)`` with sys.modules restored on exit.

    The caller sets ``sqlite_mod.get_config.return_value`` to the cfg object
    the ``/settings`` route should see, then drives the Flask test client.
    """
    snapshot = dict(sys.modules)
    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)
    mock_cfg = make_mock_cfg()
    try:
        flask_app = _load_app_module(mock_cfg, _RecordingPaho)
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        import sqlite as sqlite_mod

        yield flask_app, sqlite_mod
    finally:
        for name in list(sys.modules):
            if name not in snapshot:
                sys.modules.pop(name, None)
        for name, mod in snapshot.items():
            sys.modules[name] = mod


def login(client):
    """Authenticate the test session via the /login form post."""
    resp = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert resp.status_code in (200, 302), resp.data
