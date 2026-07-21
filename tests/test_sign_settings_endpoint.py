"""Tests for GET /api/sign/settings (issue #51).

v1 wire shape:
    {
        "target_version": "<7-char short SHA>",
        "timezone": "US/Pacific"
    }

Both fields are always concrete on the wire — Flask resolves an empty
persisted `SignSettings.target_version` to its own running short SHA
before responding. The Pi's loader uses `target_version` as the upgrade
target; `timezone` is informational (mirrors today's behavior).

The endpoint runs in parallel with the existing
`GET /api/sign/boot-config` (legacy Pis continue to call that).
New Pis call this endpoint; the legacy endpoint is unchanged.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from dataclasses import dataclass
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


@dataclass
class _FakeSignSettings:
    """A stand-in for `SignSettings` with the two fields the endpoint reads."""

    target_version: str = ""
    name: str = "Lindsay's Heart"


@dataclass
class _FakeSignConfig:
    """Stand-in for `SignConfig` exposing only `sign` and `timezone`."""

    sign: _FakeSignSettings
    timezone: str = "US/Pacific"


def _load_app_module(
    mock_cfg,
    paho_client_ctor,
    *,
    persisted_target_version: str = "",
    persisted_timezone: str = "US/Pacific",
    flask_short_sha: str = "abc1234",
):
    """Load main.py with mocked heavy deps, plus a controllable
    `sqlite.get_config()` return so the endpoint sees the
    operator-pinned / empty-persisted states we want to exercise."""
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
            import json

            return json.dumps(
                {"type": self.type, "payload": self.payload},
                separators=(",", ":"),
            )

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
    sqlite_mod.get_all_messages = MagicMock(return_value=[])
    sqlite_mod.get_messages_since = MagicMock(return_value=[])
    sqlite_mod.message_count = MagicMock(return_value=0)
    sqlite_mod.put_message = MagicMock()
    sqlite_mod.get_message = MagicMock(return_value=None)
    sqlite_mod.put_config = MagicMock()
    # The endpoint reads `cfg.sign.target_version` and `cfg.timezone`.
    # A MagicMock would fall through to the Flask-short-SHA branch; use
    # an explicit fake so we can drive both states from the test.
    fake_cfg = _FakeSignConfig(
        sign=_FakeSignSettings(target_version=persisted_target_version),
        timezone=persisted_timezone,
    )
    sqlite_mod.get_config = MagicMock(return_value=fake_cfg)
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

    # Stub `lib_shared.boot_config` so `_resolve_boot_config` (which
    # would otherwise shell out to git) returns our pinned value.
    boot_config_mod = types.ModuleType("lib_shared.boot_config")
    boot_config_mod.BootConfig = type(
        "BootConfig",
        (),
        {
            "__init__": lambda self, expected_sha: setattr(self, "expected_sha", expected_sha)
            or setattr(self, "short_sha", expected_sha[:7])
        },
    )
    boot_config_mod.from_heroku_or_git = MagicMock(
        return_value=boot_config_mod.BootConfig(expected_sha=flask_short_sha or "")
    )
    boot_config_mod.SIGN_SETTINGS_PATH = "/api/sign/settings"
    boot_config_mod.short_sha = lambda s: s[:7] if s else ""
    sys.modules["lib_shared.boot_config"] = boot_config_mod

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

    return flask_app, fake_cfg


@pytest.fixture
def flask_factory(monkeypatch):
    """Returns a callable that builds the Flask app with controllable
    persisted config and Flask running SHA. Cleanup restores real
    lib_shared.* modules so sibling tests see the genuine package.
    """
    factory_holder = {}

    def _factory(
        *,
        persisted_target_version: str = "",
        persisted_timezone: str = "US/Pacific",
        flask_short_sha: str = "abc1234",
    ):
        mock_cfg = _make_mock_cfg()
        real_modules = {}
        for name in list(sys.modules):
            if name == "lib_shared" or name.startswith("lib_shared."):
                real_modules[name] = sys.modules[name]
        monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)

        class _RecordingPaho:
            def __init__(self, dispatch_callback, **kwargs):
                self.publish_envelope = MagicMock(return_value=True)
                self.start = MagicMock()
                self.stop = MagicMock()
                factory_holder["instance"] = self

        flask_app, fake_cfg = _load_app_module(
            mock_cfg,
            _RecordingPaho,
            persisted_target_version=persisted_target_version,
            persisted_timezone=persisted_timezone,
            flask_short_sha=flask_short_sha,
        )
        flask_app.config["TESTING"] = True
        flask_app.config["WTF_CSRF_ENABLED"] = False
        return flask_app, fake_cfg

    yield _factory

    # Best-effort restore: clear any test-mocked lib_shared.* modules
    # we left behind so subsequent tests see the genuine package.
    for name in list(sys.modules):
        if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in (
            "lib_shared",
            "lib_shared.boot_config",
        ):
            # Only clear stubs (real lib_shared.* imports set real submodules)
            mod = sys.modules.get(name)
            if mod is not None and not hasattr(mod, "__file__"):
                sys.modules.pop(name, None)


@pytest.fixture
def esp32_headers():
    return {"X-API-Key": "esp32-api-key"}


# ---------------------------------------------------------------------------
# /api/sign/settings — endpoint shape (auth, response fields)
# ---------------------------------------------------------------------------


class TestSignSettingsEndpointShape:
    def test_returns_pinned_target_version(self, flask_factory, esp32_headers):
        """Operator-pinned target_version flows through to the wire."""
        flask_app, _ = flask_factory(persisted_target_version="abc1234")
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json == {
            "target_version": "abc1234",
            "timezone": "US/Pacific",
        }

    def test_resolves_empty_persisted_target_version_to_flask_sha(self, flask_factory, esp32_headers):
        """Empty persisted target_version → Flask's own running short SHA."""
        flask_app, _ = flask_factory(
            persisted_target_version="",
            flask_short_sha="deadbee",
        )
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json == {
            "target_version": "deadbee",
            "timezone": "US/Pacific",
        }

    def test_truncates_long_pinned_target_version_to_seven_chars(self, flask_factory, esp32_headers):
        """A persisted full-length (40-char) SHA is truncated to 7 chars
        on the wire — matches the worktree directory naming convention."""
        flask_app, _ = flask_factory(persisted_target_version="b5e191c5df481d51c4e7d1cced51cf7c656f1ead")
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json["target_version"] == "b5e191c"

    def test_returns_timezone_from_sign_config(self, flask_factory, esp32_headers):
        """`timezone` is read from `SignConfig.timezone` (top-level)."""
        flask_app, _ = flask_factory(
            persisted_target_version="abc1234",
            persisted_timezone="Europe/Paris",
        )
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json["timezone"] == "Europe/Paris"

    def test_default_timezone_when_sign_config_timezone_empty(self, flask_factory, esp32_headers):
        """Empty persisted timezone defaults to US/Pacific."""
        flask_app, _ = flask_factory(
            persisted_target_version="abc1234",
            persisted_timezone="",
        )
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 200
        assert response.json["timezone"] == "US/Pacific"

    def test_returns_401_without_api_key(self, flask_factory):
        flask_app, _ = flask_factory(persisted_target_version="abc1234")
        client = flask_app.test_client()
        response = client.get("/api/sign/settings")
        assert response.status_code == 401

    def test_returns_401_with_invalid_api_key(self, flask_factory):
        flask_app, _ = flask_factory(persisted_target_version="abc1234")
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers={"X-API-Key": "wrong-key"})
        assert response.status_code == 401

    def test_returns_500_when_neither_pin_nor_flask_sha_resolve(self, flask_factory, esp32_headers):
        """Both empty persisted value AND empty Flask SHA → 500."""
        flask_app, _ = flask_factory(
            persisted_target_version="",
            flask_short_sha="",  # Flask could not resolve its own SHA
        )
        client = flask_app.test_client()
        response = client.get("/api/sign/settings", headers=esp32_headers)
        assert response.status_code == 500
        assert "could not resolve target_version" in response.json["error"]


# ---------------------------------------------------------------------------
# /api/sign/settings — runs in parallel with /api/sign/boot-config
# ---------------------------------------------------------------------------


class TestParallelWithBootConfig:
    def test_boot_config_endpoint_still_present(self, flask_factory, esp32_headers):
        """The legacy /api/sign/boot-config endpoint is unchanged — the
        settings endpoint runs in parallel, NOT in place."""
        flask_app, _ = flask_factory(persisted_target_version="abc1234")
        client = flask_app.test_client()
        response = client.get("/api/sign/boot-config", headers=esp32_headers)
        assert response.status_code == 200
        assert "expected_sha" in response.json
        assert "short_sha" in response.json
