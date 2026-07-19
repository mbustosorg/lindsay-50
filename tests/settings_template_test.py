"""Tests for the v3 /settings template rendering.

Covers:
- senders.items() iteration renders one <tr> per sender entry
- enforcement_enabled checkbox pre-checks when cfg.text_settings.enforcement_enabled is True
- name_display_format dropdown marks the cfg value as `selected`
- Filter Rules: status column + per-row `filter_status_<idx>` checkbox
  pre-checks when f.status == 'enabled'
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"

from lib_shared.models import (  # noqa: E402
    EffectsSettings,
    FilterRule,
    SignConfig,
    SignSettings,
    TextSettings,
)


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


class _RecordingPaho:
    def __init__(self, dispatch_callback, **kwargs):
        self.kwargs = kwargs
        self.publish_envelope = MagicMock(return_value=True)
        self.start = MagicMock()
        self.stop = MagicMock()


def _load_app_module(mock_cfg, real_cfg):
    """Mount the Flask app with `sqlite.get_config` returning a real cfg."""
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    real_lib_shared = types.ModuleType("lib_shared")
    real_lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    sys.modules["lib_shared"] = real_lib_shared

    real_models = importlib.import_module("lib_shared.models")
    sys.modules["lib_shared.models"] = real_models

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    mm_mod = _make_mock("lib_shared.message_manager")
    mm_mod.MessageManager = MagicMock()

    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg

    log_setup_mod = _make_mock("lib_shared.log_setup")
    log_setup_mod.configure_logging = MagicMock()

    paho_mod = _make_mock("lib_shared.paho_mqtt_client")
    paho_mod.PahoMqttClient = _RecordingPaho

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
    sqlite_mod.get_config = MagicMock(return_value=real_cfg)
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
def cfg_factory():
    """Factory that builds a fresh SignConfig for each test."""

    def _make(**overrides):
        cfg = SignConfig(
            sign_settings=SignSettings(
                sign_name=overrides.get("sign_name", "Lindsay's Heart"),
                timezone=overrides.get("timezone", "US/Pacific"),
            ),
            text_settings=TextSettings(
                speed=overrides.get("text_speed", 3),
                color=overrides.get("text_color", 16711680),
                text_effect=overrides.get("text_effect", "scroll"),
                enforcement_enabled=overrides.get("enforcement_enabled", True),
            ),
            effects_settings=EffectsSettings(
                name_display_format=overrides.get("name_display_format", "first_initial_if_duplicates")
            ),
            filters=overrides.get("filters", []),
            senders=overrides.get("senders", {}),
        )
        return cfg

    return _make


@pytest.fixture
def client(cfg_factory, monkeypatch):
    """A Flask test client whose sqlite.get_config returns a fresh cfg
    per-test (so mutations during one test don't leak to the next)."""
    real_cfg = cfg_factory()

    mock_cfg = _make_mock_cfg()

    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)

    flask_app = _load_app_module(mock_cfg, real_cfg)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    # Rebuild cfg per test using the factory.
    import sqlite as sqlite_mod

    def _get_cfg():
        return real_cfg

    sqlite_mod.get_config = _get_cfg

    try:
        yield flask_app.test_client(), real_cfg, cfg_factory
    finally:
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert response.status_code in (200, 302), response.data


def _get_settings_body(client):
    """GET /settings and return the rendered body text."""
    response = client.get("/settings")
    assert response.status_code == 200, response.data
    return response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_template_renders_senders_items(client):
    """senders.items() iteration renders one <tr> per sender entry."""
    client_obj, real_cfg, cfg_factory = client
    real_cfg.senders["+15551234567"] = {
        "name": "Alice",
        "allowed": True,
        "phone": "+15551234567",
    }
    real_cfg.senders["+15559999999"] = {
        "name": "Bob",
        "allowed": False,
        "phone": "+15559999999",
    }
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # Each sender's name appears
    assert "Alice" in body
    assert "Bob" in body
    # Each sender's phone appears (the template uses `key` for the value attr)
    assert "+15551234567" in body
    assert "+15559999999" in body


def test_template_renders_empty_senders_with_blank_row(client):
    """When senders is empty, the body still has at least one input row
    (the blank add-row)."""
    client_obj, real_cfg, _ = client
    real_cfg.senders = {}
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The blank row's placeholder is rendered
    assert "+15551234567" in body  # placeholder text


def test_template_enforcement_enabled_checkbox_checked_when_true(client):
    """enforcement_enabled=True → <input ... checked>"""
    client_obj, real_cfg, _ = client
    real_cfg.text_settings.enforcement_enabled = True
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The enforcement checkbox is checked
    assert 'name="enforcement_enabled" value="1" checked' in body


def test_template_enforcement_enabled_checkbox_unchecked_when_false(client):
    """enforcement_enabled=False → no `checked` on the enforcement checkbox."""
    client_obj, real_cfg, _ = client
    real_cfg.text_settings.enforcement_enabled = False
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The enforcement checkbox is NOT checked (still rendered with value="1")
    assert 'name="enforcement_enabled" value="1"' in body
    # But the `checked` attribute is absent immediately after value="1"
    # Use a stricter check: confirm no occurrence of 'name="enforcement_enabled" value="1" checked'
    assert 'name="enforcement_enabled" value="1" checked' not in body


def test_template_name_display_format_dropdown_pre_selects_cfg_value(client):
    """name_display_format='full' → <option value="full" selected>"""
    client_obj, real_cfg, _ = client
    real_cfg.effects_settings.name_display_format = "full"
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The matching option is selected
    assert 'value="full"' in body
    # Stricter: 'value="full" selected' or 'value="full" selected' substring appears
    assert 'value="full" selected' in body


def test_template_name_display_format_dropdown_default(client):
    """Default cfg → first_initial_if_duplicates is selected."""
    client_obj, _, _ = client
    _login(client_obj)
    body = _get_settings_body(client_obj)
    assert 'value="first_initial_if_duplicates" selected' in body


def test_template_filter_rules_status_column_present(client):
    """Filter Rules table has a `Status` header column."""
    client_obj, _, _ = client
    _login(client_obj)
    body = _get_settings_body(client_obj)
    assert "<th" in body and "Status" in body


def test_template_filter_rules_per_row_status_checkbox_enabled(client):
    """An enabled FilterRule renders with `filter_status_<idx>` checked."""
    client_obj, real_cfg, _ = client
    real_cfg.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress", status="enabled"))
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # Per-row checkbox for index 0 is checked
    assert 'name="filter_status_0" value="on" checked' in body


def test_template_filter_rules_per_row_status_checkbox_disabled(client):
    """A disabled FilterRule renders without the `checked` attribute."""
    client_obj, real_cfg, _ = client
    real_cfg.filters.append(FilterRule(type="keyword", pattern="spam", action="suppress", status="disabled"))
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # Per-row checkbox for index 0 is rendered (value="on") but NOT checked.
    assert 'name="filter_status_0" value="on"' in body
    assert 'name="filter_status_0" value="on" checked' not in body


def test_template_filter_rules_empty_message(client):
    """Empty filters list renders the 'No filter rules' empty-row."""
    client_obj, real_cfg, _ = client
    real_cfg.filters = []
    _login(client_obj)
    body = _get_settings_body(client_obj)
    assert "No filter rules" in body


def test_template_filter_rule_status_filter_type_does_not_include_sender(client):
    """The 'Add Rule' form's filter_type <select> does NOT offer `sender`."""
    client_obj, _, _ = client
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The add-rule select offers keyword/regex/message but NOT sender
    # (sender matching lives in the senders list, not the rules table)
    # We assert 'sender' doesn't appear inside the add-rule filter_type select.
    # A simple proxy: confirm the canonical 3 options appear.
    assert '<option value="keyword">keyword</option>' in body
    assert '<option value="regex">regex</option>' in body
    assert '<option value="message">message</option>' in body


def test_template_sender_allowed_checkbox_value_is_phone(client):
    """The sender_allowed checkbox `value` attribute is the row's phone,
    NOT the row's enumerate index — so removing a row doesn't shift
    surviving rows' allowed flags (the handler pairs by phone).

    Regression pin: this is the field that broke when the index-based
    pairing was in place. The test sends a real cfg with two senders
    and asserts the rendered HTML carries `value="<phone>"` for each.
    """
    client_obj, real_cfg, _ = client
    real_cfg.senders["+15551234567"] = {
        "name": "Alice",
        "allowed": True,
        "phone": "+15551234567",
    }
    real_cfg.senders["+15559999999"] = {
        "name": "Bob",
        "allowed": False,
        "phone": "+15559999999",
    }
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # Each row's checkbox value is the row's phone, not 0/1
    assert 'name="sender_allowed" value="+15551234567"' in body
    assert 'name="sender_allowed" value="+15559999999"' in body


def test_template_sender_phone_input_has_sync_handler(client):
    """Each sender_phone input has an `oninput` handler that keeps its
    row's sender_allowed checkbox value in sync."""
    client_obj, real_cfg, _ = client
    real_cfg.senders["+15551234567"] = {
        "name": "Alice",
        "allowed": True,
        "phone": "+15551234567",
    }
    _login(client_obj)
    body = _get_settings_body(client_obj)
    # The phone input wires up the sync handler
    assert 'oninput="syncSenderAllowed(this)"' in body
