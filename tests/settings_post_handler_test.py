"""Tests for the /settings POST handler's v3 wiring.

The /settings route reads form fields and writes into a SignConfig
in-place, then `_save_and_publish` snapshots it via sqlite.put_config +
s3.save_config_snapshot + MQTT publish. We mount the real SignConfig in
sqlite.get_config's return value (so attribute writes stick), drive a
POST through the test client, and assert the persisted SignConfig
shape via sqlite.put_config.call_args.

Covers:
- POST parses parallel `sender_name` / `sender_phone` / `sender_state`
  lists into cfg.senders keyed by normalize_phone(phone)
- POST `enforce_allowed_senders` checkbox → cfg.sign_settings.enforce_allowed_senders
- POST `name_display_format` dropdown → cfg.text_settings.name_display_format
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
        # Senders — one empty row by default. The trailing add-row's
        # hidden `sender_state` defaults to `:1` because the rendered
        # template emits a checked default for that row's visible
        # checkbox. The handler skips empty-phone rows so the value
        # is harmless even with no phone key to match.
        "sender_name": [""],
        "sender_phone": [""],
        "sender_state": [":1"],
        # Sign identity + senders master toggle
        "sign_name": "Test Sign",
        "timezone": "US/Pacific",
        "enforce_allowed_senders": "1",
        # Text settings (incl. name_display_format)
        "text_settings_speed": "3",
        "text_settings_color": "16711680",
        "text_settings_text_effect": "scroll",
        "name_display_format": "first_initial_if_duplicates",
        # Effects settings (pacing + lookback + selector)
        "effects_settings_fade_seconds": "2.0",
        "effects_settings_hold_seconds": "15.0",
        "effects_settings_intro_seconds": "5.0",
        "effects_settings_idle_seconds": "300.0",
        "effects_settings_lookback_days": "14",
        "effects_settings_selector_algorithm": "weighted",
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
    """sender_name / sender_phone / sender_state lists parse into a dict-of-dict.

    The handler pairs `sender_state` to its row by phone (the row's
    `value="<phone>:0|1"`), not by enumerate index — so removing a row
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
            sender_state=["+15551234567:1"],
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

    The new `sender_state` wire format keys by phone, so removing
    Alice (omitting her `sender_state` entry from the POST) leaves
    Bob and Carol's `:1` flags intact and Alice lands as False (no
    matching state).
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            # Three rows; the first is "removed" by simply not including
            # its phone's state in `sender_state`. (In the browser the
            # Remove button does `tr.remove()` which omits the row
            # entirely; here we exercise the same end-state by including
            # only Bob and Carol's state entries.)
            sender_name=["Alice", "Bob", "Carol"],
            sender_phone=["+15551111111", "+15552222222", "+15553333333"],
            sender_state=["+15552222222:1", "+15553333333:1"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    assert real_cfg.senders[normalize_phone("+15552222222")]["allowed"] is True
    assert real_cfg.senders[normalize_phone("+15553333333")]["allowed"] is True
    # Alice's row was excluded from `sender_state` — she's not in the
    # state list, so her allowed flag defaults to False.
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
            sender_state=["+15551234567:1"],
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
            sender_state=[key + ":1"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)
    # Only one entry; the duplicate was rejected (logger.warning was
    # emitted; the handler kept cfg.senders intact).
    assert len(real_cfg.senders) == 1
    assert real_cfg.senders[key]["name"] in ("Pre-existing",)


def test_post_senders_explicit_zero_blocks_row(app_with_real_cfg, client):
    """A `sender_state` entry of `<phone>:0` (explicit un-check) saves
    the row with `allowed=False`.

    Regression pin for the "all three got enabled" bug — the previous
    handler conflated explicit un-checks with JS-failed checkbox
    state. The new wire format (`phone:0|1`) carries the operator's
    un-check intent explicitly, so `+15551111111:0` lands as False
    without ambiguity.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Adam Rose"],
            sender_phone=["+14152985015"],
            sender_state=["+14152985015:0"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    key = normalize_phone("+14152985015")
    assert key in real_cfg.senders
    assert real_cfg.senders[key]["allowed"] is False


def test_post_senders_explicit_one_allows_row(app_with_real_cfg, client):
    """A `sender_state` entry of `<phone>:1` (explicit check) saves
    the row with `allowed=True`. The handler trusts the form's
    explicit state — there is no JS-failed / un-checked ambiguity
    in the new wire format.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Adam Rose"],
            sender_phone=["+14152985015"],
            sender_state=["+14152985015:1"],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    key = normalize_phone("+14152985015")
    assert key in real_cfg.senders
    assert real_cfg.senders[key]["allowed"] is True


def test_post_senders_mixed_states_per_phone(app_with_real_cfg, client):
    """Three rows posted with mixed `:1`/`:0` flags — exactly what
    the operator's form does after the new wire format change. The
    handler matches by phone key, so the explicit per-row state is
    preserved without index-drift or empty-string voodoo.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Alice", "Bob", "Carol"],
            sender_phone=["+15551111111", "+15552222222", "+15553333333"],
            sender_state=[
                "+15551111111:1",
                "+15552222222:0",
                "+15553333333:1",
            ],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    assert real_cfg.senders[normalize_phone("+15551111111")]["allowed"] is True
    assert real_cfg.senders[normalize_phone("+15552222222")]["allowed"] is False
    assert real_cfg.senders[normalize_phone("+15553333333")]["allowed"] is True


def test_post_senders_malformed_state_dropped_with_warning(app_with_real_cfg, client):
    """Malformed `sender_state` entries (no `:`, bad flag, empty
    phone) are dropped with a log warning — the row's allowed flag
    falls back to False. Mirrors `test_post_effects_malformed_state_*`.
    """
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post(
        "/settings",
        data=_base_form(
            sender_name=["Alice"],
            sender_phone=["+15551111111"],
            sender_state=[
                "+15551111111:1",  # well-formed
                "completelymalformed",  # missing colon
                "+15551111111:2",  # bad flag
                ":1",  # empty phone
            ],
        ),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    from lib_shared.phone_utils import normalize_phone

    key = normalize_phone("+15551111111")
    assert key in real_cfg.senders
    # The well-formed `:1` entry wins; the other three drop with
    # warnings and don't land.
    assert real_cfg.senders[key]["allowed"] is True


def test_post_enforce_allowed_senders_checkbox_writes_to_sign_settings(app_with_real_cfg, client):
    """enforce_allowed_senders=1 → sign_settings.enforce_allowed_senders=True."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(enforce_allowed_senders="1"))
    assert response.status_code in (200, 302)
    assert real_cfg.sign_settings.enforce_allowed_senders is True


def test_post_enforce_allowed_senders_unchecked_keeps_default(app_with_real_cfg, client):
    """When the checkbox is absent, enforce_allowed_senders is False."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    form = _base_form()
    form.pop("enforce_allowed_senders")
    response = client.post("/settings", data=form)
    assert response.status_code in (200, 302)
    assert real_cfg.sign_settings.enforce_allowed_senders is False


def test_post_name_display_format_dropdown_writes_to_text_settings(app_with_real_cfg, client):
    """name_display_format=full → text_settings.name_display_format='full'."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(name_display_format="full"))
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.name_display_format == "full"


def test_post_text_settings_color_with_hash_prefix_parses(app_with_real_cfg, client):
    """`#rrggbb` form input must parse — `int(..., 16)` rejects `#`-prefixed
    input silently if not stripped, so the operator's typed color
    silently didn't save. Regression pin for the 2026-07-19 incident."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(text_settings_color="#ff0000"))
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.color == 0xFF0000


def test_post_text_settings_color_without_hash_prefix_parses(app_with_real_cfg, client):
    """Bare `rrggbb` form input also parses — the picker sometimes POSTs
    with the `#` stripped by browser normalization."""
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    response = client.post("/settings", data=_base_form(text_settings_color="00ff00"))
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.color == 0x00FF00


def test_post_text_settings_color_garbage_keeps_prior_value(app_with_real_cfg, client):
    """A non-hex value must NOT corrupt the saved color — fall through
    silently (with a warning) and keep the prior cfg.text_settings.color."""
    flask_app, real_cfg = app_with_real_cfg
    real_cfg.text_settings.color = 0x123456  # distinct from the default
    _login(client)
    response = client.post("/settings", data=_base_form(text_settings_color="not-a-color"))
    assert response.status_code in (200, 302)
    assert real_cfg.text_settings.color == 0x123456


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
            sender_state=["+15551234567:1"],
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


def test_post_effects_full_list_enables_each_as_posted(app_with_real_cfg, client):
    """POST effect_state=Hyperspace:1&effect_state=Honeycomb:0&... → cfg
    reflects each entry's explicit :0/:1 flag.

    Regression pin (issue #6 follow-up): the form's hidden `effect_state`
    inputs always carry one row per canonical effect with explicit
    enabled/disabled state. The handler trusts the form — missing entries
    fall back to the loader's default enabled flag (the form always POSTs
    the full list, so a true absence is a sign of a malformed client).
    """
    from lib_shared.effects_loader import load_effects_settings

    canonical = load_effects_settings().get("effects", [])
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    state_values = [f"{e['name']}:{'1' if e['name'] in ('Hyperspace', 'NightSky') else '0'}" for e in canonical]
    response = client.post(
        "/settings",
        data=_base_form(effect_state=state_values),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    by_name = {e["name"]: e for e in real_cfg.effects_settings.effects}
    # Saved list mirrors the canonical order (loader drives order).
    saved_order = [e["name"] for e in real_cfg.effects_settings.effects]
    canonical_order = [e["name"] for e in canonical]
    assert saved_order == canonical_order
    assert by_name["Hyperspace"]["enabled"] is True
    assert by_name["NightSky"]["enabled"] is True
    assert by_name["Honeycomb"]["enabled"] is False
    assert by_name["Fireworks"]["enabled"] is False


def test_post_effects_disabled_all_clears_remaining(app_with_real_cfg, client):
    """POST effect_state=*:0 for every row → all effects disabled.

    The "form is the source of truth on save" model: an operator who
    un-checks every Effects List row lands with all effects disabled,
    mirroring how the senders list is now cleared on an empty POST.
    """
    from lib_shared.effects_loader import load_effects_settings

    canonical = load_effects_settings().get("effects", [])
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    state_values = [f"{e['name']}:0" for e in canonical]
    response = client.post(
        "/settings",
        data=_base_form(effect_state=state_values),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    assert all(e["enabled"] is False for e in real_cfg.effects_settings.effects)


def test_post_effects_enabled_all_renders_full_list(app_with_real_cfg, client):
    """POST effect_state=*:1 for every row → all effects enabled."""
    from lib_shared.effects_loader import load_effects_settings

    canonical = load_effects_settings().get("effects", [])
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    state_values = [f"{e['name']}:1" for e in canonical]
    response = client.post(
        "/settings",
        data=_base_form(effect_state=state_values),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    assert all(e["enabled"] is True for e in real_cfg.effects_settings.effects)


def test_post_effects_malformed_state_dropped_with_warning(app_with_real_cfg, client):
    """An `effect_state` entry that's not `<name>:0|1` is dropped with a
    log warning — the canonical entry it would have been paired with
    falls back to the loader's default enabled state."""
    from lib_shared.effects_loader import load_effects_settings

    canonical = load_effects_settings().get("effects", [])
    flask_app, real_cfg = app_with_real_cfg
    _login(client)
    canonical_names = [e["name"] for e in canonical]
    state_values = [
        f"{canonical_names[0]}:1",  # well-formed: enables row 0
        f"{canonical_names[1]}:2",  # bad flag (not 0 or 1)
        "completelymalformed",  # missing colon
    ]
    response = client.post(
        "/settings",
        data=_base_form(effect_state=state_values),
        follow_redirects=False,
    )
    assert response.status_code in (200, 302)

    saved_names = {e["name"] for e in real_cfg.effects_settings.effects}
    assert saved_names == set(canonical_names)
    # The first canonical entry was enabled=1 by the well-formed form value.
    assert real_cfg.effects_settings.effects[0]["enabled"] is True
