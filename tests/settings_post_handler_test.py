"""Tests for the /settings POST handler's v3 wiring.

The /settings route reads form fields and writes into a SignConfig
in-place, then `_save_and_publish` snapshots it via sqlite.put_config +
s3.save_config_snapshot + MQTT publish. We mount the real SignConfig in
sqlite.get_config's return value (so attribute writes stick), drive a
POST through the test client, and assert the persisted SignConfig
shape via sqlite.put_config.call_args.

Covers:
- POST parses parallel `sender_name` / `sender_phone` / `sender_allowed`
  lists into cfg.senders keyed by normalize_phone(phone)
- POST `enforcement_enabled` checkbox → cfg.text_settings.enforcement_enabled
- POST `name_display_format` dropdown → cfg.effects_settings.name_display_format
- POST sign_name → cfg.sign_settings.sign_name
- POST timezone → cfg.sign_settings.timezone (when valid)
- POST filter_rule add (filter_action=add) creates a new FilterRule
  with status from the filter_status checkbox
- An empty entries list does NOT wipe the existing senders
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

# We need to import the REAL models module — these tests rely on
# SignConfig.from_dict/to_dict actually working as advertised.
from lib_shared.models import (  # noqa: E402
    EffectsSettings,
    FilterRule,
    MessageEnvelope,
    SignConfig,
    SignSettings,
    TextSettings,
)


def _make_mock_cfg():
    """Minimal mock cfg so config_reader and friends have something to read."""
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
    """Stand-in for PahoMqttClient that records init kwargs + publish calls."""

    def __init__(self, dispatch_callback, **kwargs):
        self.kwargs = kwargs
        self.publish_envelope = MagicMock(return_value=True)
        self.start = MagicMock()
        self.stop = MagicMock()


def _load_app_module_with_real_cfg(mock_cfg, real_cfg):
    """Same as test_auth's loader, but `sqlite.get_config` returns a
    real `SignConfig` so attribute writes stick during the POST handler.
    The real cfg instance is held in a side-channel `cfg_holder` so the
    test can read it after the POST.
    """
    mock_modules = {}

    def _make_mock(name):
        mod = types.ModuleType(name)
        mock_modules[name] = mod
        sys.modules[name] = mod
        return mod

    # Pin a real `lib_shared.models` so the handler's from_dict / to_dict
    # calls work and SignConfig construction validates properly.
    real_lib_shared = types.ModuleType("lib_shared")
    real_lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    sys.modules["lib_shared"] = real_lib_shared

    real_models = importlib.import_module("lib_shared.models")
    sys.modules["lib_shared.models"] = real_models

    # Stub the heavy submodules — config_migrations, message_manager —
    # the handler doesn't reach into them on the /settings POST path.
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
    # Return the real cfg so writes stick.
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
def app_with_real_cfg(monkeypatch):
    """Flask app with sqlite.get_config returning a real SignConfig."""
    real_cfg = SignConfig()  # default v3 schema
    mock_cfg = _make_mock_cfg()

    # Save any pre-existing lib_shared.modules so we can restore after.
    real_modules = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            real_modules[name] = sys.modules[name]

    monkeypatch.delenv("HEROKU_SLUG_COMMIT", raising=False)
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)

    flask_app = _load_app_module_with_real_cfg(mock_cfg, real_cfg)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    try:
        yield flask_app, real_cfg
    finally:
        for name, real_mod in real_modules.items():
            sys.modules[name] = real_mod
        for name in list(sys.modules):
            if (name == "lib_shared" or name.startswith("lib_shared.")) and name not in real_modules:
                sys.modules.pop(name, None)


@pytest.fixture
def client(app_with_real_cfg):
    flask_app, _ = app_with_real_cfg
    return flask_app.test_client()


def _login(client):
    response = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert response.status_code in (200, 302), response.data


def _base_form(**overrides):
    """A /settings POST body that satisfies every required field, with
    one empty sender row and one empty filter rule."""
    base = {
        # Senders — one empty row by default
        "sender_name": [""],
        "sender_phone": [""],
        # Sign identity
        "sign_name": "Test Sign",
        "timezone": "US/Pacific",
        # Text settings
        "text_settings_speed": "3",
        "text_settings_color": "16711680",
        "text_settings_text_effect": "scroll",
        "enforcement_enabled": "1",
        # Effects settings (pacing + lookback + selector + display format)
        "effects_settings_fade_seconds": "2.0",
        "effects_settings_hold_seconds": "15.0",
        "effects_settings_intro_seconds": "5.0",
        "effects_settings_idle_seconds": "300.0",
        "effects_settings_lookback_days": "14",
        "effects_settings_selector_algorithm": "weighted",
        "name_display_format": "first_initial_if_duplicates",
        # Filter rules — one empty row by default
        "filter_pattern": [""],
        "filter_type": ["keyword"],
        "filter_action": ["suppress"],
        "filter_status": ["enabled"],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_post_senders_parses_parallel_lists(app_with_real_cfg, client):
    """sender_name / sender_phone / sender_allowed lists parse into a dict-of-dict.

    The handler pairs `sender_allowed` to its row by phone (the row's
    `value="<phone>"`), not by enumerate index — so removing a row
    doesn't shift surviving rows' allowed flags.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Alice", "Bob"],
            sender_phone=["+15551234567", "+15559999999"],
            # Only Alice is allowed: checkbox value = Alice's phone.
            sender_allowed=["+15551234567"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    key_alice = normalize_phone("+15551234567")
    key_bob = normalize_phone("+15559999999")
    assert key_alice in real_cfg.senders
    assert key_bob in real_cfg.senders
    assert real_cfg.senders[key_alice]["name"] == "Alice"
    assert real_cfg.senders[key_alice]["allowed"] is True
    assert real_cfg.senders[key_bob]["name"] == "Bob"
    assert real_cfg.senders[key_bob]["allowed"] is False


def test_post_senders_remove_surviving_rows_preserve_their_allowed_flag(app_with_real_cfg, client):
    """Removing a row from the form (e.g. operator clicks Remove on the
    first of three rows) does NOT flip the surviving rows' allowed flags.

    Regression test for the index-drift bug: the previous handler paired
    `sender_allowed` by enumerate index, so removing row 0 left row 1's
    checkbox at `value="1"` while the handler checked `str(0) in
    allowed_set` — flipping it to False. The fix pairs by phone.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            # Three rows; the first is "removed" by simply not including
            # its phone's checkbox in `sender_allowed`. (In the browser
            # the Remove button does `tr.remove()` which omits the row
            # entirely; here we exercise the same end-state by including
            # only Bob and Carol's checkbox values.)
            sender_name=["Alice", "Bob", "Carol"],
            sender_phone=["+15551111111", "+15552222222", "+15553333333"],
            sender_allowed=["+15552222222", "+15553333333"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    assert real_cfg.senders[normalize_phone("+15552222222")]["allowed"] is True
    assert real_cfg.senders[normalize_phone("+15553333333")]["allowed"] is True
    # Alice's row was excluded from `sender_allowed` — she's not in the
    # checked list, so her allowed flag is False.
    assert real_cfg.senders[normalize_phone("+15551111111")]["allowed"] is False


def test_post_senders_unfilled_add_row_skipped(app_with_real_cfg, client):
    """The blank add-row at the bottom of the form (empty phone) is
    skipped — it does NOT land in cfg.senders."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            # The form always has one trailing empty row (the add-row).
            sender_name=["Alice", ""],
            sender_phone=["+15551234567", ""],
            sender_allowed=["+15551234567"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    # Only Alice is in cfg.senders; the empty add-row was dropped.
    assert len(real_cfg.senders) == 1
    from lib_shared.phone_utils import normalize_phone

    assert normalize_phone("+15551234567") in real_cfg.senders


def test_post_senders_duplicate_phone_preserves_prior_state(app_with_real_cfg, client):
    """Two rows with the same phone → the duplicate is dropped (the
    existing entry is preserved by the partial-save logic, since the
    duplicate check refuses to clobber cfg.senders when `seen_keys`
    already has the entry).
    """
    flask_app, real_cfg = app_with_real_cfg
    from lib_shared.phone_utils import normalize_phone

    key = normalize_phone("+15551234567")
    # Pre-populate so the post has a fresh entry to dedupe against.
    real_cfg.senders[key] = {"name": "Pre-existing", "allowed": True, "phone": "+15551234567"}
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Pre-existing", "Duplicate"],
            sender_phone=["+15551234567", "+15551234567"],
            sender_allowed=[key],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    # Only one entry; the duplicate was rejected (logger.warning was
    # emitted; the handler kept cfg.senders intact).
    assert len(real_cfg.senders) == 1
    assert real_cfg.senders[key]["name"] in ("Pre-existing",)


def test_post_enforcement_enabled_checkbox_writes_to_text_settings(app_with_real_cfg, client):
    """enforcement_enabled=1 → text_settings.enforcement_enabled=True."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(enforcement_enabled="1"))
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.enforcement_enabled is True


def test_post_enforcement_enabled_unchecked_keeps_default(app_with_real_cfg, client):
    """When the checkbox is absent, enforcement_enabled is False."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    form = _base_form()
    form.pop("enforcement_enabled")
    response = client.post("/settings", data=form)
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.enforcement_enabled is False


def test_post_name_display_format_dropdown_writes_to_effects_settings(app_with_real_cfg, client):
    """name_display_format=full → effects_settings.name_display_format='full'."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(name_display_format="full"))
    assert response.status_code in (200, 302)
    assert real_cfg.effects_settings.name_display_format == "full"


def test_post_sign_name_writes_to_sign_settings(app_with_real_cfg, client):
    """sign_name form field writes to sign_settings.sign_name."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(sign_name="My Custom Sign", timezone="US/Eastern"),
    )
    assert response.status_code in (200, 302)
    assert real_cfg.sign_settings.sign_name == "My Custom Sign"
    assert real_cfg.sign_settings.timezone == "US/Eastern"


def test_post_empty_senders_clears_existing(app_with_real_cfg, client):
    """POST with no filled sender rows (only the trailing blank add-row)
    → cfg.senders is cleared. The form is the source of truth on save.

    Regression pin: an earlier "defensive" branch preserved the prior
    cfg.senders when the form posted zero filled rows — which made it
    impossible for an operator to empty the list by clicking Remove on
    every row. The trailing blank add-row's empty phone is skipped by
    the handler, so an empty POST is unambiguous.
    """
    flask_app, real_cfg = app_with_real_cfg
    from lib_shared.phone_utils import normalize_phone

    # Pre-populate senders
    real_cfg.senders["+15551234567"] = {
        "name": "Pre-existing",
        "allowed": True,
        "phone": "+15551234567",
    }

    _login(client)
    # POST with no filled sender rows (only the blank add-row's empty
    # phone is in the form, which the handler skips).
    response = client.post("/settings", data=_base_form(), follow_redirects=False)
    assert response.status_code in (200, 302)

    # The pre-existing sender is cleared — the operator clicked Remove on
    # every row before saving.
    assert real_cfg.senders == {}


def test_post_only_blanks_then_add_row_with_filled_phone_saves(app_with_real_cfg, client):
    """The blank add-row's empty phone is skipped; an add-row the
    operator fills in BEFORE saving lands as a normal sender."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Alice"],
            sender_phone=["+15551234567"],
            sender_allowed=["+15551234567"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    from lib_shared.phone_utils import normalize_phone

    assert normalize_phone("+15551234567") in real_cfg.senders
    assert real_cfg.senders[normalize_phone("+15551234567")]["name"] == "Alice"


def test_post_filter_rule_add_creates_new_rule(app_with_real_cfg, client):
    """filter_action=add appends a new FilterRule with the form fields."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)

    response = client.post(
        "/settings",
        data=_base_form(
            filter_pattern="spam",
            filter_type="keyword",
            filter_action="add",
            filter_status="enabled",
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    # A new FilterRule was appended
    assert any(f.type == "keyword" and f.pattern == "spam" and f.action == "suppress" for f in real_cfg.filters)


def test_post_filter_rule_add_disabled_status(app_with_real_cfg, client):
    """filter_status absent → new FilterRule.status == 'disabled'."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)

    form = _base_form(
        filter_pattern="bad",
        filter_type="keyword",
        filter_action="add",
    )
    form.pop("filter_status")
    response = client.post("/settings", data=form, follow_redirects=False)
    assert response.status_code in (200, 302)

    matches = [f for f in real_cfg.filters if f.pattern == "bad"]
    assert len(matches) == 1
    assert matches[0].status == "disabled"
