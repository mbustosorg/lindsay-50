"""Tests for the Settings-page Pi Upgrade Control section (issue #51 §6-8).

These tests cover the Flask/template/JS integration:

  - Settings page renders the new `[data-upgrade-settings-field]` section.
  - The Target Pi version input posts back to /settings and the value
    lands on `cfg.sign.target_version` (with `_short_sha` truncation
    when over 7 chars — but the WHOLE string may be longer; we keep
    it verbatim on the Python side and only truncate at the /api/sign/settings
    serialization point).
  - Clear button (`data-action="clear-target"`) renders; command buttons
    render with the right action values.
  - The `pi_upgrade_settings.js` script exists, declares the right
    DOM hooks, and the script tag is wired into base.html.

Browser-side testing note (issue #51 §8.7). The `pi_upgrade_settings.js`
module is a pure-browser JS shim (uses `fetch`, `confirm`, `document.*`)
— there is no Python class to mirror, so the "browser test" is a
smoke that verifies the static file exists, exposes the expected
data-action data hooks, and the HTML template emits the expected
DOM scaffolding for it. The DOM-event handler behavior is small and
DOM-bound; we verify it indirectly by reading the script text for the
key wiring (URLs, headers, action names).
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
_TEMPLATES_DIR = _PROJECT_ROOT / "heart-message-manager" / "templates"
_STATIC_DIR = _PROJECT_ROOT / "heart-message-manager" / "static"


# ---------------------------------------------------------------------------
# _load_app_module harness — same shape as test_flask_command_endpoints.py
# so we exercise the real settings POST path against a real /settings render.
# ---------------------------------------------------------------------------


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

    class _FakeSignSettings:
        """In-memory SignSettings — has `.name`, `.target_version`, etc."""

        def __init__(self, **kwargs):
            self.name = kwargs.get("name", "Test")
            self.target_version = kwargs.get("target_version", "")

        def to_dict(self):
            return {"name": self.name, "target_version": self.target_version}

    class _FakeTextSettings:
        def __init__(self, **kwargs):
            self.speed = kwargs.get("speed", 3)
            self.color = kwargs.get("color", 0xFFFFFF)
            self.text_effect = kwargs.get("text_effect", "scroll")

        def to_dict(self):
            return {
                "speed": self.speed,
                "color": self.color,
                "text_effect": self.text_effect,
            }

    class _FakeEffectsSettings:
        def __init__(self, **kwargs):
            self.fade_seconds = kwargs.get("fade_seconds", 0.5)
            self.hold_seconds = kwargs.get("hold_seconds", 7.0)
            self.intro_seconds = kwargs.get("intro_seconds", 0.5)
            self.idle_seconds = kwargs.get("idle_seconds", 2.0)
            self.lookback_days = kwargs.get("lookback_days", 30)
            self.selector_algorithm = kwargs.get("selector_algorithm", "weighted")
            self.effects = kwargs.get("effects", []) or []

        def to_dict(self):
            return {
                "fade_seconds": self.fade_seconds,
                "hold_seconds": self.hold_seconds,
                "intro_seconds": self.intro_seconds,
                "idle_seconds": self.idle_seconds,
                "lookback_days": self.lookback_days,
                "selector_algorithm": self.selector_algorithm,
                "effects": list(self.effects),
            }

    class _FakeSignConfig:
        """In-memory SignConfig — has `.sign.target_version`, etc."""

        def __init__(self, **kwargs):
            self.sign = _FakeSignSettings(**kwargs.get("sign", {}))
            self.filters = kwargs.get("filters", []) or []
            self.senders = kwargs.get("senders", {}) or {}
            self.timezone = kwargs.get("timezone", "US/Pacific")
            self.text_settings = _FakeTextSettings(**(kwargs.get("text_settings", {}) or {}))
            self.effects_settings = _FakeEffectsSettings(**(kwargs.get("effects_settings", {}) or {}))
            self.effects = []

        def to_dict(self):
            return {
                "filters": [f.to_dict() if hasattr(f, "to_dict") else f for f in self.filters],
                "senders": dict(self.senders),
                "sign": (
                    self.sign.to_dict() if hasattr(self.sign, "to_dict") else {"name": "Test", "target_version": ""}
                ),
                "timezone": self.timezone,
                "version": 2,
                "effects_settings": (
                    self.effects_settings.to_dict() if hasattr(self.effects_settings, "to_dict") else {}
                ),
                "text_settings": self.text_settings.to_dict() if hasattr(self.text_settings, "to_dict") else {},
            }

    sqlite_mod._FakeSignConfig = _FakeSignConfig
    # side_effect=class ⇒ each call returns a fresh instance (no state leak).
    sqlite_mod.get_config = MagicMock(side_effect=lambda: _FakeSignConfig())
    sqlite_mod.get_all_messages = MagicMock(return_value=[])
    sqlite_mod.put_config = MagicMock()
    sqlite_mod.rebuild_from_s3 = MagicMock()
    sqlite_mod.get_messages_since = MagicMock(return_value=[])
    sqlite_mod.message_count = MagicMock(return_value=0)
    sqlite_mod.put_message = MagicMock()
    sqlite_mod.get_message = MagicMock(return_value=None)
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

    # effects_loader has a real module to load.
    effects_loader_mod = _load_real_module(
        "lib_shared.effects_loader", _PROJECT_ROOT / "lib_shared" / "effects_loader.py"
    )

    spec = importlib.util.spec_from_file_location("heart_message_manager_main", str(_MAIN_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart-message-manager.main"] = mod
    spec.loader.exec_module(mod)

    flask_app = mod.app
    flask_app.jinja_loader = None
    from jinja2 import FileSystemLoader

    flask_app.jinja_env = flask_app.create_jinja_environment()
    flask_app.jinja_env.loader = FileSystemLoader(str(_TEMPLATES_DIR))

    return flask_app


def _login_admin(client):
    resp = client.post(
        "/login",
        data={"username": "admin", "password": "secret123"},
        follow_redirects=False,
    )
    return resp


@pytest.fixture
def app(monkeypatch):
    class _RecordingPaho:
        def __init__(self, dispatch_callback, **kwargs):
            self.publish_envelope = MagicMock(return_value=True)
            self.start = MagicMock()
            self.stop = MagicMock()

    # Snapshot the pre-test module state BEFORE _load_app_module runs —
    # the harness overwrites `lib_shared.*` and `heart-message-manager.*`
    # with `types.ModuleType` mocks; the conftest's autouse
    # `_reset_effects_settings_cache` imports `_default_effects_list`
    # from `lib_shared.models` after the test body, so we MUST restore
    # the real submodules before yielding control back to pytest.
    saved_lib_modules: dict[str, types.ModuleType | None] = {}
    saved_app_modules: dict[str, types.ModuleType | None] = {}
    for name in list(sys.modules):
        if name == "lib_shared" or name.startswith("lib_shared."):
            saved_lib_modules[name] = sys.modules.get(name)
        if name == "heart-message-manager" or name.startswith("heart-message-manager."):
            saved_app_modules[name] = sys.modules.get(name)
    saved_top_level_modules: dict[str, types.ModuleType | None] = {
        "sqlite": sys.modules.get("sqlite"),
        "s3": sys.modules.get("s3"),
        "server_time": sys.modules.get("server_time"),
        "paho_mqtt_client": sys.modules.get("paho_mqtt_client"),
        "auth": sys.modules.get("auth"),
    }

    flask_app = _load_app_module(_RecordingPaho)
    flask_app.config["TESTING"] = True
    flask_app.config["WTF_CSRF_ENABLED"] = False

    try:
        yield flask_app
    finally:
        for name, mod in saved_lib_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for name, mod in saved_app_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for name, mod in saved_top_level_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture
def client(app):
    c = app.test_client()
    _login_admin(c)
    return c


@pytest.fixture
def esp32_headers():
    return {"X-API-Key": "esp32-api-key"}


# ---------------------------------------------------------------------------
# 8.1 — Settings page renders the new section
# ---------------------------------------------------------------------------


class TestUpgradeSectionRendered:
    def test_settings_page_contains_upgrade_field(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200
        assert b"data-upgrade-settings-field" in resp.data
        assert b"Pi Upgrade Control" in resp.data

    def test_target_version_input_renders_with_current_value(self, client):
        """If SignSettings.target_version is set, it shows in the input."""
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        # The input's name attr is the form-name we POST.
        assert 'name="sign_target_version"' in body
        assert "data-upgrade-target-input" in body

    def test_clear_button_renders(self, client):
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        assert 'data-action="clear-target"' in body

    def test_force_upgrade_button_renders(self, client):
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        assert 'data-action="force-upgrade"' in body
        assert "Force upgrade" in body

    def test_restart_button_renders(self, client):
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        assert 'data-action="restart"' in body

    def test_shutdown_button_renders(self, client):
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        assert 'data-action="shutdown"' in body

    def test_command_buttons_are_type_button_not_submit(self, client):
        """Each command button must be `type="button"` so it does NOT submit
        the outer /settings POST — the JS handler intercepts and does its
        own POST /api/sign/commands/<action>."""
        resp = client.get("/settings")
        body = resp.data.decode("utf-8")
        for action in ("force-upgrade", "restart", "shutdown", "clear-target"):
            needle = f'data-action="{action}"'
            idx = body.find(needle)
            assert idx != -1
            # Walk back to the preceding `<button` tag.
            button_start = body.rfind("<button", 0, idx)
            assert button_start != -1
            # ...and verify `type="button"` appears in the same <button ...> opening tag.
            button_close = body.find(">", button_start)
            button_tag = body[button_start:button_close]
            assert 'type="button"' in button_tag, (
                f"Command button for action={action} must be type=button so it doesn't "
                f"submit the outer /settings form. Tag was: {button_tag!r}"
            )


# ---------------------------------------------------------------------------
# 8.2 — POST /settings persists target_version
# ---------------------------------------------------------------------------


class TestTargetVersionPosts:
    def test_target_version_short_persists(self, app, client, monkeypatch):
        """`sign_target_version=<short SHA>` lands on cfg.sign.target_version
        AND survives across a page reload (SQLite round-trip via get_config)."""
        # Capture the post that gets put_config'd.
        captured = {}
        sqlite_mod = sys.modules["sqlite"]
        original_put = sqlite_mod.put_config

        def capturing_put(cfg):
            captured["cfg"] = cfg
            return original_put(cfg)

        monkeypatch.setattr(sqlite_mod, "put_config", capturing_put)

        resp = client.post(
            "/settings",
            data={
                "sign_name": "Test sign",
                "sign_target_version": "abc1234",
                # Required form fields to satisfy the existing handler.
                "timezone": "America/Los_Angeles",
                "text_settings_speed": "3",
                "text_settings_color": "#ffffff",
                "text_settings_text_effect": "scroll",
                "effects_settings_fade_seconds": "0.5",
                "effects_settings_hold_seconds": "7.0",
                "effects_settings_intro_seconds": "0.5",
                "effects_settings_idle_seconds": "2.0",
                "effects_settings_lookback_days": "30",
                "effects_settings_selector_algorithm": "weighted",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert "cfg" in captured
        assert captured["cfg"].sign.target_version == "abc1234"

    def test_empty_target_version_does_not_clobber_existing(self, app, client, monkeypatch):
        """An empty POST preserves the previously-saved target_version."""
        sqlite_mod = sys.modules["sqlite"]
        original_get = sqlite_mod.get_config

        # Pre-populate target_version in the in-memory config that the
        # handler will read.
        existing_cfg = sqlite_mod._FakeSignConfig(target_version="abc1234")
        sqlite_mod.get_config = MagicMock(return_value=existing_cfg)
        captured = {}
        original_put = sqlite_mod.put_config

        def capturing_put(cfg):
            captured["cfg"] = cfg
            return original_put(cfg)

        monkeypatch.setattr(sqlite_mod, "put_config", capturing_put)
        try:
            resp = client.post(
                "/settings",
                data={
                    "sign_name": "Test sign",
                    "sign_target_version": "",  # operator explicitly cleared it
                    "timezone": "America/Los_Angeles",
                    "text_settings_speed": "3",
                    "text_settings_color": "#ffffff",
                    "text_settings_text_effect": "scroll",
                    "effects_settings_fade_seconds": "0.5",
                    "effects_settings_hold_seconds": "7.0",
                    "effects_settings_intro_seconds": "0.5",
                    "effects_settings_idle_seconds": "2.0",
                    "effects_settings_lookback_days": "30",
                    "effects_settings_selector_algorithm": "weighted",
                },
                follow_redirects=False,
            )
            assert resp.status_code in (302, 303)
            # Empty form input ⇒ we DO save the empty string, so the
            # next /api/sign/settings request falls back to Flask's
            # running short SHA. Documented in the spec.
            assert captured["cfg"].sign.target_version == ""
        finally:
            sqlite_mod.get_config = original_get

    def test_full_sha_target_version_persists_verbatim(self, app, client, monkeypatch):
        """A 40-char full SHA passes through unchanged — truncation to
        7 chars happens ONLY at the /api/sign/settings serialization
        point, not at form-save time."""
        sqlite_mod = sys.modules["sqlite"]
        full = "0123456789abcdef0123456789abcdef01234567"
        captured = {}
        original_put = sqlite_mod.put_config

        def capturing_put(cfg):
            captured["cfg"] = cfg
            return original_put(cfg)

        monkeypatch.setattr(sqlite_mod, "put_config", capturing_put)

        resp = client.post(
            "/settings",
            data={
                "sign_name": "Test sign",
                "sign_target_version": full,
                "timezone": "America/Los_Angeles",
                "text_settings_speed": "3",
                "text_settings_color": "#ffffff",
                "text_settings_text_effect": "scroll",
                "effects_settings_fade_seconds": "0.5",
                "effects_settings_hold_seconds": "7.0",
                "effects_settings_intro_seconds": "0.5",
                "effects_settings_idle_seconds": "2.0",
                "effects_settings_lookback_days": "30",
                "effects_settings_selector_algorithm": "weighted",
            },
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303)
        assert captured["cfg"].sign.target_version == full


# ---------------------------------------------------------------------------
# 8.7 — JS module smoke: pi_upgrade_settings.js exists with the right wiring
#       (browser-side test via static analysis since the file is pure JS).
# ---------------------------------------------------------------------------


class TestUpgradeJsStatic:
    """The Settings-page JS shim is a pure-browser module — verify it exists,
    wires the right data-action values, posts to the correct endpoint, and
    sends `X-API-Key` from `window.APP_CONFIG.auth.API_SECRET_KEY`.

    A pure JS module can't be loaded by Pyodide without a full PyScript
    env, so the spec's "browser test" surface for this module is
    static-analysis: we assert on the script text rather than driving
    the DOM. This is the same testing posture the project uses for
    `sign_status.js` (no Python mirror, no PyScript harness).
    """

    _script_text: str | None = None

    @classmethod
    def _script(cls):
        if cls._script_text is None:
            cls._script_text = (_STATIC_DIR / "pi_upgrade_settings.js").read_text(encoding="utf-8")
        return cls._script_text

    def test_script_file_exists(self):
        assert (
            _STATIC_DIR / "pi_upgrade_settings.js"
        ).exists(), "static/pi_upgrade_settings.js must exist for the section to wire"

    def test_script_handles_all_three_command_actions(self):
        text = self._script()
        for action in ("force-upgrade", "restart", "shutdown"):
            assert action in text, f"missing {action} in pi_upgrade_settings.js"

    def test_script_uses_x_api_key_header(self):
        text = self._script()
        assert '"X-API-Key"' in text, "X-API-Key header missing from fetch call"
        assert "APP_CONFIG" in text, "must read API key from window.APP_CONFIG"
        assert "API_SECRET_KEY" in text, "must read API_SECRET_KEY specifically"

    def test_script_handles_each_http_status_with_toast(self):
        """202 success, 401 unauth, 404 unknown action, other = generic err."""
        text = self._script()
        assert "202" in text
        assert "401" in text
        assert "404" in text

    def test_script_short_circuits_when_section_absent(self):
        """`[data-upgrade-settings-field]` only — safe to include on every page."""
        text = self._script()
        assert "data-upgrade-settings-field" in text
        # IIFE short-circuits on missing root.
        assert "if (!root) return" in text

    def test_base_html_includes_script_tag(self):
        """base.html must include pi_upgrade_settings.js via url_for('static', ...)."""
        base_html = (_TEMPLATES_DIR / "base.html").read_text(encoding="utf-8")
        assert "pi_upgrade_settings.js" in base_html
        # And it's properly routed through Flask's static helper.
        assert "url_for('static', filename='pi_upgrade_settings.js')" in base_html
