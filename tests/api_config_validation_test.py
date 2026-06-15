"""Tests for the Flask PUT /api/config validation path (v2 effect_settings + text_settings).

Covers:
- Valid v2 payloads are accepted (HTTP 200)
- v1 payloads (with tz_offset_mins + rendering) are migrated and accepted
- effect_settings validation: malformed entries, unknown names, out-of-range
- text_settings validation: out-of-range fields, unknown text_effect, bad color

Uses the `app` + `client` + `esp32_headers` fixtures from test_auth.py
to spin up a Flask app with all heavy deps mocked. The PUT /api/config
endpoint is exercised via the test client.
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on the path so lib_shared is importable
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# --- App + client fixtures (mirrors test_auth.py) -------------------------


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
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    # Mock ONLY the heavy lib_shared submodules. The real `lib_shared` package
    # + `lib_shared.models` + `lib_shared.config_migrations` are needed as-is
    # by `_build_sign_config_from_request`.
    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg
    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()
    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = MagicMock()

    import importlib.util as _util

    def _load_real_module(name, path):
        spec = _util.spec_from_file_location(name, str(path))
        mod = _util.module_from_spec(spec)
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

    paho_top_mod = types.ModuleType("paho_mqtt_client")
    paho_top_mod.PahoMqttClient = MagicMock()
    sys.modules["paho_mqtt_client"] = paho_top_mod

    import importlib.util

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


@pytest.fixture
def app():
    """Create a test Flask app with all heavy deps mocked."""
    mock_cfg = _make_mock_cfg()
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
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def esp32_headers():
    return {"X-API-Key": "esp32-api-key"}


# --- valid payloads ---


def test_valid_v2_payload_returns_200(client, esp32_headers):
    """A complete v2 payload yields HTTP 200 and an 'ok' status."""
    data = {
        "version": 2,
        "filters": [],
        "senders": [],
        "sign": {"name": "OK"},
        "timezone": "America/Los_Angeles",
        "effect_settings": {
            "effects": [{"name": "Hyperspace", "enabled": True}],
            "fade_seconds": 2.0,
            "hold_seconds": 15.0,
            "intro_seconds": 5.0,
            "idle_seconds": 300.0,
            "recent_count": 5,
        },
        "text_settings": {
            "frame_delay": 0.04,
            "offset_seconds": 1.0,
            "color": 0xFF0000,
            "text_effect": "scroll",
        },
    }
    response = client.put("/api/config", json=data, headers=esp32_headers)
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_valid_v1_payload_is_migrated_then_accepted(client, esp32_headers):
    """A v1 payload is migrated to v2 and accepted (HTTP 200)."""
    data = {
        "version": 1,
        "filters": [],
        "senders": [],
        "sign": {"name": "Old"},
        "timezone": "US/Pacific",
        "tz_offset_mins": -420,
        "rendering": {"mode": "scroll", "speed": 0.5, "color": 0xFFFFFF},
    }
    response = client.put("/api/config", json=data, headers=esp32_headers)
    assert response.status_code == 200


# --- effect_settings validation ---


def test_effect_settings_must_be_object(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": "nope"},
        headers=esp32_headers,
    )
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_effect_settings_effects_must_be_list(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"effects": "not a list"}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_entry_must_be_dict(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"effects": [42]}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_entry_must_have_name_string(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"effects": [{"enabled": True}]}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_entry_must_have_enabled_bool(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"effects": [{"name": "Hyperspace", "enabled": "yes"}]}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_unknown_effect_name_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"effects": [{"name": "NotAnEffect", "enabled": True}]}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_known_effect_names_accepted(client, esp32_headers):
    for name in [
        "Hyperspace",
        "VideoDisplay",
        "PngDisplay",
        "Honeycomb",
        "Flame",
        "Fireworks",
        "NightSky",
    ]:
        response = client.put(
            "/api/config",
            json={"effect_settings": {"effects": [{"name": name, "enabled": True}]}},
            headers=esp32_headers,
        )
        assert response.status_code == 200, f"{name} unexpectedly rejected"


def test_effect_settings_negative_pacing_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"fade_seconds": -1.0}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_zero_pacing_accepted(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={
            "effect_settings": {
                "effects": [{"name": "Hyperspace", "enabled": True}],
                "fade_seconds": 0.0,
                "hold_seconds": 0.0,
                "intro_seconds": 0.0,
                "idle_seconds": 0.0,
            }
        },
        headers=esp32_headers,
    )
    assert response.status_code == 200


def test_effect_settings_recent_count_zero_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"recent_count": 0}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_effect_settings_recent_count_negative_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": {"recent_count": -1}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


# --- text_settings validation ---


def test_text_settings_must_be_object(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": "nope"},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_negative_frame_delay_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"frame_delay": -0.01}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_negative_offset_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"offset_seconds": -1.0}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_color_too_high_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"color": 0x01000000}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_color_negative_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"color": -1}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_color_zero_accepted(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"color": 0}},
        headers=esp32_headers,
    )
    assert response.status_code == 200


def test_text_settings_color_max_accepted(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"color": 0xFFFFFF}},
        headers=esp32_headers,
    )
    assert response.status_code == 200


def test_text_settings_unknown_text_effect_rejected(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"text_effect": "spiral"}},
        headers=esp32_headers,
    )
    assert response.status_code == 400


def test_text_settings_scroll_accepted(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"text_settings": {"text_effect": "scroll"}},
        headers=esp32_headers,
    )
    assert response.status_code == 200


# --- error response shape ---


def test_error_response_has_error_key(client, esp32_headers):
    response = client.put(
        "/api/config",
        json={"effect_settings": "not an object"},
        headers=esp32_headers,
    )
    body = response.get_json()
    assert "error" in body
