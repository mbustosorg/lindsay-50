"""Tests for GET /api/sign/expected-sha and the Flask auto-reboot publish.

Covers issue #49: version coordination between Flask and the Pi's
loader. The endpoint is the source of truth for "what SHA should the
Pi be running?" — Heroku's `HEROKU_SLUG_COMMIT` in production,
`git rev-parse HEAD` from the repo root in local dev. The auto-reboot
publish (on every MQTT connect) is what tells the Pi to come back
and check; without it the Pi would only ever re-sync on a manual
reboot.

The `_load_app_module` harness is shared with `test_auth.py` —
heavy deps (sqlite, s3, paho network, MQTT broker) are mocked so
the tests can drive Flask in-process without ever connecting to
anything.
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
    """Return a mock config object with the keys Flask's main.py reads.

    Mirrors `_make_mock_cfg` in test_auth.py so the two suites share
    the same starting state. The `if_exists` shim returns the same
    auth credentials the device would use.
    """
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
    """Load main.py using importlib, mocking heavy deps and capturing the paho client.

    Mirrors `_load_app_module` from test_auth.py but installs a
    caller-supplied `paho_client_ctor` so each test can capture
    the constructor arguments (and the `on_connect_callback` that
    proves the auto-reboot publish is wired up).

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
        """Capture the args the Flask main.py passes to MessageEnvelope(...)"""

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

    # The paho client module's PahoMqttClient is what the test wants
    # to inspect — install the caller-supplied constructor so each
    # test can capture the wiring (specifically the on_connect_callback).
    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = paho_client_ctor

    # Mock heart-message-manager submodules — the real auth module
    # is loaded so the API key gate works.
    def _load_real_module(name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
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
    """Flask app + the captured PahoMqttClient constructor + the captured MQTT client instance.

    The MQTT client mock records every call to `publish_envelope` so
    tests can assert on what Flask sent on connect. The `on_connect_callback`
    is also captured so tests can trigger it directly to verify it
    publishes a `command=reboot` envelope.
    """
    captured = {}

    class _RecordingPaho:
        """Stand-in for PahoMqttClient that captures its constructor args."""

        def __init__(self, dispatch_callback, **kwargs):
            captured["dispatch_callback"] = dispatch_callback
            captured["kwargs"] = kwargs
            captured["on_connect_callback"] = kwargs.get("on_connect_callback")
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()
            captured["instance"] = self

    mock_cfg = _make_mock_cfg()

    # Save the real lib_shared submodules BEFORE _load_app_module
    # replaces them with mocks. See test_auth.py for the same dance.
    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    # Ensure the env doesn't pollute the expected_sha tests unless they
    # explicitly set it.
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
# /api/sign/expected-sha — env var set (Heroku)
# ---------------------------------------------------------------------------


class TestExpectedShaSlugCommit:
    def test_returns_slug_commit_when_set(self, app, client, esp32_headers, monkeypatch):
        """HEROKU_SLUG_COMMIT takes precedence over the git fallback."""
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get("/api/sign/expected-sha", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json == {"expected_sha": "abc1234567890"}

    def test_returns_401_without_api_key(self, app, client, monkeypatch):
        """Request without X-API-Key returns 401."""
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get("/api/sign/expected-sha")
        assert response.status_code == 401

    def test_returns_401_with_invalid_api_key(self, app, client, monkeypatch):
        """Wrong X-API-Key value returns 401."""
        monkeypatch.setenv("HEROKU_SLUG_COMMIT", "abc1234567890")
        response = client.get(
            "/api/sign/expected-sha",
            headers={"X-API-Key": "not-the-right-key"},
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# /api/sign/expected-sha — env var unset (local dev: git rev-parse fallback)
# ---------------------------------------------------------------------------


class TestExpectedShaGitFallback:
    def test_returns_local_git_head_when_slug_unset(
        self, app, client, esp32_headers, monkeypatch
    ):
        """When HEROKU_SLUG_COMMIT is not set, return the local git HEAD SHA."""
        # Already cleared by the app fixture; explicitly set None just to be sure.
        monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
        response = client.get("/api/sign/expected-sha", headers=esp32_headers)
        assert response.status_code == 200
        # The repo has a real HEAD — the SHA is a 40-char hex string.
        sha = response.json["expected_sha"]
        assert isinstance(sha, str)
        assert len(sha) >= 7  # git short or full SHA both work
        int(sha, 16)  # raises if non-hex


# ---------------------------------------------------------------------------
# Auto-reboot publish on Flask startup
# ---------------------------------------------------------------------------


class TestStartupPublishesReboot:
    def test_paho_client_constructed_with_on_connect_callback(self, app):
        """PahoMqttClient is constructed with an on_connect_callback wired to publish reboot."""
        _, captured = app
        # main.py constructs PahoMqttClient exactly once at module load.
        assert captured["on_connect_callback"] is not None
        assert callable(captured["on_connect_callback"])

    def test_on_connect_callback_publishes_command_reboot_envelope(self, app):
        """Triggering the on_connect callback publishes exactly one command=reboot envelope."""
        _, captured = app
        cb = captured["on_connect_callback"]
        assert cb is not None
        cb()  # simulate a successful MQTT CONNACK
        instance = captured["instance"]
        # Exactly one publish_envelope call
        assert instance.publish_envelope.call_count == 1
        # The published envelope is the reboot command
        args, _ = instance.publish_envelope.call_args
        env = args[0]
        assert env.type == "command"
        assert env.payload == {"action": "reboot"}

    def test_on_connect_callback_called_multiple_times_publishes_each_time(self, app):
        """Each connect (initial + reconnects) publishes one reboot envelope (idempotent on the Pi)."""
        _, captured = app
        cb = captured["on_connect_callback"]
        cb()
        cb()
        cb()
        instance = captured["instance"]
        # Three callbacks, three publish_envelope calls (per scenario:
        # "When Flask restarts and the MQTT client reconnects, then one
        # command=reboot envelope is published on cfg.MQTT_TOPIC after the
        # reconnection completes")
        assert instance.publish_envelope.call_count == 3
        for call in instance.publish_envelope.call_args_list:
            env = call.args[0]
            assert env.type == "command"
            assert env.payload == {"action": "reboot"}

    def test_on_connect_callback_swallows_publish_failure(self, app):
        """A publish_envelope failure does not propagate — Flask must keep running."""
        _, captured = app
        instance = captured["instance"]
        instance.publish_envelope.return_value = False
        cb = captured["on_connect_callback"]
        # Must not raise
        cb()