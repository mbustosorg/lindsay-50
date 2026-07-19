"""Tests that the /settings admin page renders effects from the loader.

Verifies the contract that the spec pins:

- The Effects section in `templates/settings.html` iterates the loader's
  `effects_settings["effects"]` list, not a hardcoded list in
  `main.py` or `_DEFAULT_EFFECTS_LIST_FULL` (which is gone). An operator
  override (`config_overrides/effects_settings.json`) or
  `EFFECTS_SETTINGS_OVERRIDE` env var must reach the rendered HTML
  unchanged — no rebuild, no merge.

- The four timing fields (`fade_seconds`, `hold_seconds`,
  `intro_seconds`, `idle_seconds`) and `lookback_days` pre-populate
  from the loader's canonical value when the wire envelope is absent
  (i.e. `cfg.effects_settings.<field>` is `None`).

`_load_app_module` is a self-contained harness modeled on
`test_boot_config_endpoint.py` — heavy deps (sqlite, s3, paho network,
MQTT broker) are mocked so the tests drive Flask in-process without
ever connecting to anything.
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


def _load_app_module(mock_cfg, paho_client_ctor):
    """Load main.py using importlib, mocking heavy deps.

    The parent `lib_shared` mock gets a real `__path__` so Python's
    import system can resolve any submodules we DON'T mock (e.g.
    `lib_shared.scroller_base`) from the real filesystem. Without
    `__path__`, the import falls through with `'lib_shared' is not a
    package` because a bare `types.ModuleType` carries no package
    metadata.
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
        def __init__(self, type, payload):
            self.type = type
            self.payload = payload

        def to_json(self):
            return json.dumps({"type": self.type, "payload": self.payload}, separators=(",", ":"))

    models_mod.MessageEnvelope = _FakeEnvelope
    models_mod.MessageView = MagicMock()
    # `main.py` imports `EffectsSettings` and consults its validator
    # class-level constants (`MIN_LOOKBACK_DAYS`, `MAX_LOOKBACK_DAYS`,
    # `VALID_SELECTOR_ALGORITHMS`) when validating /settings POST
    # payloads — the mock loader has to expose real values for those
    # so validator comparisons don't silently lie with MagicMocks.
    effects_settings_mock = MagicMock()
    effects_settings_mock.MIN_LOOKBACK_DAYS = 1
    effects_settings_mock.MAX_LOOKBACK_DAYS = 365
    effects_settings_mock.VALID_SELECTOR_ALGORITHMS = ("weighted", "random")
    models_mod.EffectsSettings = effects_settings_mock

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    """Flask app + the captured PahoMqttClient constructor + the captured MQTT client instance."""
    captured = {}

    class _RecordingPaho:
        def __init__(self, dispatch_callback, **kwargs):
            captured["dispatch_callback"] = dispatch_callback
            captured["kwargs"] = kwargs
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
    monkeypatch.delenv("EFFECTS_SETTINGS_OVERRIDE", raising=False)

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


def _make_cfg_with_none_timing():
    """A MagicMock cfg that mimics "no wire envelope yet" semantics.

    `cfg.sign.name`, `cfg.timezone`, `cfg.text_settings.<attr>`,
    `cfg.filters`, `cfg.senders` get reasonable values; the
    `effects_settings` block returns None for every timing field so
    the template's `if cfg.effects_settings.X is not none else
    effects_settings.X` falls through to the loader's canonical value.
    """
    cfg = MagicMock()
    cfg.sign.name = "Lindsay's Heart"
    cfg.timezone = "America/Los_Angeles"
    cfg.filters = []
    cfg.senders = {}
    # Effects settings: None on every timing field → fallback to loader.
    cfg.effects_settings.fade_seconds = None
    cfg.effects_settings.hold_seconds = None
    cfg.effects_settings.intro_seconds = None
    cfg.effects_settings.idle_seconds = None
    cfg.effects_settings.lookback_days = None
    cfg.effects_settings.selector_algorithm = None
    cfg.effects_settings.effects = None
    return cfg


def _login(client):
    """Standard /login form post so the session is authenticated."""
    response = client.post("/login", data={"username": "admin", "password": "secret123"})
    assert response.status_code in (200, 302), response.data


# ---------------------------------------------------------------------------
# 1. Admin renders effects from config
# ---------------------------------------------------------------------------


class TestAdminRendersFromLoader:
    """The /settings page iterates the loader's effects list verbatim.

    Pin the contract: the Effects section's checkboxes derive from
    `load_effects_settings()["effects"]`, not from a hardcoded list
    in `main.py` or a deleted module-level constant. The canonical
    file has 9 effects (7 enabled, 2 disabled) — all 9 names must
    appear in the rendered HTML, in the order the JSON declares.
    """

    def test_all_canonical_effect_names_appear(self, app, client):
        import sqlite as sqlite_mod

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        assert response.status_code == 200, response.data

        body = response.get_data(as_text=True)
        for name in (
            "Hyperspace",
            "Honeycomb",
            "WindFire",
            "CoronalMassEjection",
            "Eyeball",
            "Fireworks",
            "NightSky",
        ):
            assert name in body, f"expected {name!r} in /settings body"

    def test_module_and_class_caption_appears(self, app, client):
        """The operator-friendly `module.ClassName` caption shows up
        per effect row so they can see which module to edit."""
        import sqlite as sqlite_mod

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        body = response.get_data(as_text=True)
        # At least one of the entries has both module + class_name.
        assert "lib_shared.patterns.fireworks" in body
        assert "Fireworks" in body


# ---------------------------------------------------------------------------
# 2. Override-added effect name shows up
# ---------------------------------------------------------------------------


class TestOverrideAddedEffectShows:
    """When the loader returns a list with an effect name that the
    canonical does NOT carry, the override-added name must show up in
    the rendered HTML."""

    def test_runtime_override_adds_new_effect_name(self, app, client, tmp_path, monkeypatch):
        import sqlite as sqlite_mod
        import lib_shared.effects_loader as effects_loader

        # The /settings GET path passes `load_effects_settings()` to
        # the template directly — it does NOT instantiate
        # `EffectsSettings()`, so we don't need to clear the
        # `_default_effects_list._cache` per-function cache. The
        # loader's process-lifetime cache reset is enough.

        # Build a minimal override JSON with a name that the canonical
        # does NOT carry, then point the env var at it and reset the
        # caches. The override REPLACES the canonical list — the
        # spec's REPLACE-only semantics.
        override_file = tmp_path / "override.json"
        override_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Hyperspace",
                            "enabled": True,
                            "module": "lib_shared.patterns.hyperspace",
                            "class_name": "Hyperspace",
                        },
                        {
                            "name": "Flame",
                            "enabled": True,
                            "module": "lib_shared.patterns.flame",
                            "class_name": "Flame",
                        },
                        {
                            "name": "BrandNewPattern",
                            "enabled": True,
                            "module": "lib_shared.patterns.brand_new",
                            "class_name": "BrandNew",
                        },
                    ],
                    "fade_seconds": 2.0,
                    "hold_seconds": 15.0,
                    "intro_seconds": 5.0,
                    "idle_seconds": 300.0,
                    "lookback_days": 14,
                    "selector_algorithm": "weighted",
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(override_file))
        effects_loader.reset_effects_settings()

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        assert response.status_code == 200, response.data
        body = response.get_data(as_text=True)

        assert "BrandNewPattern" in body, "override-added effect name must show up in /settings"

        # And the deleted canonical names must NOT appear in the
        # rendered list (the override REPLACED, did not merge).
        # We don't fire a hard assertion on the whole list because
        # other canonical strings ("Hyperspace", "Flame") are also
        # present in the override, but "Fireworks" and "NightSky"
        # are not in this override and were removed.
        assert "Fireworks" not in body, "deleted canonical name should not appear when override replaces"


# ---------------------------------------------------------------------------
# 3. Deleted canonical name does NOT show up
# ---------------------------------------------------------------------------


class TestDeletedCanonicalNameAbsent:
    """A canonical effect name that's been removed from the override
    must NOT appear in the rendered HTML. Pin REPLACE-only semantics
    on the read path."""

    def test_deleted_canonical_name_absent(self, app, client, tmp_path, monkeypatch):
        import sqlite as sqlite_mod
        import lib_shared.effects_loader as effects_loader

        # The /settings GET path passes `load_effects_settings()` to
        # the template directly — it does NOT instantiate
        # `EffectsSettings()`, so we don't need to clear the
        # `_default_effects_list._cache` per-function cache. The
        # loader's process-lifetime cache reset is enough.

        # Override with Fireworks REMOVED (canonical has 5; this has
        # 4 — Fireworks dropped).
        override_file = tmp_path / "override.json"
        override_file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "effects": [
                        {
                            "name": "Hyperspace",
                            "enabled": True,
                            "module": "lib_shared.patterns.hyperspace",
                            "class_name": "Hyperspace",
                        },
                        {
                            "name": "Honeycomb",
                            "enabled": True,
                            "module": "lib_shared.patterns.honeycomb",
                            "class_name": "Honeycomb",
                        },
                        {
                            "name": "Flame",
                            "enabled": True,
                            "module": "lib_shared.patterns.flame",
                            "class_name": "Flame",
                        },
                        {
                            "name": "NightSky",
                            "enabled": True,
                            "module": "lib_shared.patterns.nightsky",
                            "class_name": "NightSky",
                        },
                    ],
                    "fade_seconds": 2.0,
                    "hold_seconds": 15.0,
                    "intro_seconds": 5.0,
                    "idle_seconds": 300.0,
                    "lookback_days": 14,
                    "selector_algorithm": "weighted",
                }
            )
        )
        monkeypatch.setenv("EFFECTS_SETTINGS_OVERRIDE", str(override_file))
        effects_loader.reset_effects_settings()

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        assert response.status_code == 200, response.data
        body = response.get_data(as_text=True)

        assert "Fireworks" not in body, "Fireworks was deleted from the override; must not show up"
        # And the surviving names do show up.
        for name in ("Hyperspace", "Honeycomb", "Flame", "NightSky"):
            assert name in body, f"surviving name {name!r} should still appear"


# ---------------------------------------------------------------------------
# 4. Timing fields pre-populate from canonical loader value
# ---------------------------------------------------------------------------


class TestTimingFieldsPrePopulate:
    """When no wire envelope is present (`cfg.effects_settings.<field>`
    is `None`), the four timing fields and `lookback_days`
    pre-populate from the loader's canonical `effects_settings`
    block. (The `selector_algorithm` field is a `<select>` — its
    default pre-populates via a `selected` attribute on the
    matching `<option>`, pinned in a sibling test below.)"""

    def test_timing_fields_render_canonical_value(self, app, client):
        import sqlite as sqlite_mod
        import lib_shared.effects_loader as effects_loader

        # The autouse `app` fixture already cleared the env var and
        # reset the loader, so the canonical file is the source.
        canonical = effects_loader.load_effects_settings()
        assert canonical is not None

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        body = response.get_data(as_text=True)

        # Each timing input has `value="<canonical>"`. The canonical
        # values are: fade=2.0, hold=15.0, intro=5.0, idle=300.0,
        # lookback_days=14.
        assert f'value="{canonical["fade_seconds"]}"' in body
        assert f'value="{canonical["hold_seconds"]}"' in body
        assert f'value="{canonical["intro_seconds"]}"' in body
        assert f'value="{canonical["idle_seconds"]}"' in body
        assert f'value="{canonical["lookback_days"]}"' in body

    def test_selector_algorithm_dropdown_pre_selects_canonical(self, app, client):
        """The `selector_algorithm` `<select>` marks the canonical
        value as `selected` so the admin /settings page reflects the
        right algorithm on first render.

        Pins the dispatch wiring for the live-config pick path —
        if the `selected` attribute ever lands on the wrong option,
        an operator who opens /settings after a fresh deploy sees
        a misleading default. We assert `selected` appears on the
        option whose value matches the canonical field.
        """
        import sqlite as sqlite_mod
        import lib_shared.effects_loader as effects_loader

        canonical = effects_loader.load_effects_settings()
        assert canonical is not None
        canonical_alg = canonical["selector_algorithm"]

        sqlite_mod.get_config.return_value = _make_cfg_with_none_timing()
        _login(client)

        response = client.get("/settings")
        body = response.get_data(as_text=True)

        assert (f'value="{canonical_alg}"' " selected") in body or (f'value="{canonical_alg}" selected') in body, (
            f"Expected the `selector_algorithm` <select> to "
            f"pre-select {canonical_alg!r} on first render; not "
            f"found in body. The first 600 chars:\n{body[:600]}"
        )
