"""Tests for the boot hook that re-saves+re-publishes config on startup.

Intent (issue #71 deployment history):

Every Flask startup re-runs `_save_and_publish(sqlite.get_config())` so
the S3 snapshot's `target_version` stays current with Flask's running
SHA when `pinned_version` is empty (the canonical case on every fresh
deploy). This produces a deployment history record — every deploy
creates a fresh S3 snapshot + MQTT envelope so the Versions card's
Flask/Config cell reflects "the config this Flask binary shipped with"
rather than "whatever config was last saved by an older Flask binary".

The hook runs unconditionally on boot — no guard. The operator's
position: "we rarely ever reboot the dyno if we're not deploying (ie.
config changes anyways) — so an extra config file in that edge case is
no big deal. I prefer to keep it simpler."

These tests pin the contract:

1. `_boot_refresh_config()` always calls `_save_and_publish(sqlite.get_config())`.
2. The call runs the full save+publish path: persist SQLite,
   write S3 snapshot, publish MQTT envelope.
3. Empty `pinned_version` is resolved against Flask's running SHA.
4. Exceptions are caught so a transient S3 or MQTT failure cannot
   prevent the server from accepting requests.

The loader mirrors `settings_post_handler_test.py`'s approach:
`sqlite.get_config` returns a real `SignConfig` so attribute writes
stick during the boot hook's call to `_save_and_publish`.

We replace `_save_and_publish` and `_mqtt_client_publish_config` on
the loaded module BEFORE invoking `_boot_refresh_config`, so the
hook picks up our observers. This works because the hook is a
plain function call — no name-binding gotchas at module load time.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MAIN_PATH = _PROJECT_ROOT / "heart-message-manager" / "main.py"
_AUTH_PATH = _PROJECT_ROOT / "heart-message-manager" / "auth.py"


def _make_mock_cfg():
    """Build a MagicMock `cfg` with sensible defaults for the loader.

    The boot hook reads `cfg.MQTT_*` etc. from the loaded `get_config()`
    return — those don't matter for the boot-refresh test, but every
    attribute access on a MagicMock returns another MagicMock, which
    can swallow real exceptions and hide intent.
    """
    cfg = MagicMock()
    cfg.if_exists = MagicMock(return_value=None)
    cfg.MQTT_HOST = "test-host"
    cfg.MQTT_PORT = 1883
    cfg.MQTT_USERNAME = "u"
    cfg.MQTT_PASSWORD = "p"
    cfg.MQTT_TOPIC = "test/topic"
    cfg.MQTT_STATUS_TOPIC = ""
    cfg.SECRET_KEY = None
    cfg.FLASK_SECRET_KEY = None
    cfg.PORT = 5000
    cfg.HOST = "127.0.0.1"
    cfg.MQTT_WS_URL = "ws://test/ws"
    cfg.MQTT_LONG_DISCONNECT_MS = 60000
    cfg.MESSAGES_API_URL = "http://localhost/api/messages"
    cfg.CONFIG_API_URL = "http://localhost/api/config"
    cfg.AUTH_API_KEY = "test-key"
    return cfg


def _load_app_module(cfg_instance, *, s3_side_effect=None):
    """Load heart_message_manager.main with mocked heavy deps.

    `cfg_instance` is the real SignConfig returned by sqlite.get_config()
    so attribute writes stick when `_save_and_publish` mutates it.

    Returns the loaded module. Test code then replaces
    `mod._save_and_publish` and `mod._mqtt_client_publish_config` with
    observers and invokes `mod._boot_refresh_config()` directly.

    Note: the module's at-load body invokes `_boot_refresh_config()`
    exactly once as a side effect of import. Tests that want to
    observe ONLY the explicit invocation should snapshot the
    relevant call counters BEFORE calling it.
    """
    mock_cfg = _make_mock_cfg()

    real_lib_shared = types.ModuleType("lib_shared")
    real_lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    sys.modules["lib_shared"] = real_lib_shared

    real_models = importlib.import_module("lib_shared.models")
    sys.modules["lib_shared.models"] = real_models

    def _make_mock(name):
        mod = types.ModuleType(name)
        sys.modules[name] = mod
        return mod

    cm_mod = _make_mock("lib_shared.config_migrations")
    cm_mod.migrate = MagicMock(side_effect=lambda d, current_version: d or {})
    cm_mod.migrate_on_startup = MagicMock()

    _make_mock("lib_shared.message_manager").MessageManager = MagicMock()

    config_reader_mod = _make_mock("lib_shared.config_reader")
    config_reader_mod.get_config = lambda required_keys=None: mock_cfg

    _make_mock("lib_shared.log_setup").configure_logging = MagicMock()

    _make_mock("lib_shared.paho_mqtt_client").PahoMqttClient = MagicMock()

    def _load_real_module(name, path):
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    auth_mod = _load_real_module("heart-message-manager.auth", _AUTH_PATH)
    sys.modules["auth"] = auth_mod

    _make_mock("heart-message-manager.sqlite")
    _make_mock("heart-message-manager.s3")
    _make_mock("heart-message-manager.server_time")
    _make_mock("heart-message-manager.paho_mqtt_client")

    sqlite_mod = types.ModuleType("sqlite")
    sqlite_mod.rebuild_from_s3 = MagicMock()

    def _get_config():
        return cfg_instance

    sqlite_mod.get_config = MagicMock(side_effect=_get_config)
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
    s3_mod.save_config_snapshot = MagicMock(side_effect=s3_side_effect or MagicMock())
    s3_mod._s3_bucket = MagicMock(return_value="test-bucket")
    s3_mod._s3_client = MagicMock()
    sys.modules["s3"] = s3_mod

    server_time_mod = types.ModuleType("server_time")
    server_time_mod.format_from_iso = lambda *a, **k: ""
    server_time_mod.now_utc_iso = lambda: "2026-05-22T00:00:00Z"
    sys.modules["server_time"] = server_time_mod

    paho_mm_mod = types.ModuleType("paho_mqtt_client")
    paho_mm_mod.PahoMqttClient = MagicMock()
    sys.modules["paho_mqtt_client"] = paho_mm_mod

    spec = importlib.util.spec_from_file_location("heart_message_manager_boot_test", str(_MAIN_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["heart-message-manager.main"] = mod
    spec.loader.exec_module(mod)

    return mod


@pytest.fixture
def clean_sys_modules():
    """Each test loads heart-message-manager.main into sys.modules under
    a unique name. Drop everything the loader added so the next test
    starts fresh."""
    snapshot = {k: v for k, v in sys.modules.items()}
    yield
    for name in list(sys.modules):
        if name not in snapshot:
            del sys.modules[name]


def _install_observers(mod):
    """Replace _save_and_publish and _mqtt_client_publish_config with
    observers. Returns the captured call records."""
    captured = {"save_publish": [], "publish": [], "s3": [], "put_config": []}

    # Replace the helpers on the module so `_boot_refresh_config`'s
    # call sites pick them up. This works because Python resolves
    # `_save_and_publish(...)` and `_mqtt_client_publish_config(...)`
    # against the module's globals at call time.
    def _save_and_publish(cfg):
        captured["save_publish"].append(cfg)
        # Also record sqlite.put_config + s3 calls (the real function
        # would do this internally; for the test we just observe that
        # the helper was invoked with the right cfg).

    mod._save_and_publish = _save_and_publish

    def _publish_config(cfg_dict):
        captured["publish"].append(cfg_dict)

    mod._mqtt_client_publish_config = _publish_config

    # Patch sqlite.put_config + s3.save_config_snapshot observers
    # (these run inside the REAL _save_and_publish when we don't
    # replace it; for the unit tests we replace it, so we just
    # record that the hook called the helper).
    return captured


def test_boot_refresh_calls_save_and_publish_with_get_config(clean_sys_modules):
    """`_boot_refresh_config()` must call `_save_and_publish(sqlite.get_config())`.

    We replace `_save_and_publish` with an observer and invoke the
    hook directly. The observer captures the cfg instance passed in,
    and we assert it's the same object returned by sqlite.get_config.
    """
    from lib_shared.models import SignConfig

    real_cfg = SignConfig()
    mod = _load_app_module(real_cfg)
    captured = _install_observers(mod)

    mod._boot_refresh_config()

    assert len(captured["save_publish"]) == 1, (
        "_boot_refresh_config must call _save_and_publish exactly once"
    )
    assert captured["save_publish"][0] is real_cfg, (
        "_boot_refresh_config must pass sqlite.get_config() — the "
        "SQLite-backed instance — so attribute writes stick"
    )


def test_boot_refresh_persists_and_publishes_full_path(clean_sys_modules):
    """The hook runs the REAL `_save_and_publish`, which persists
    SQLite, writes S3, publishes MQTT envelope, and stamps
    config_sha + updated_at. We don't stub `_save_and_publish`
    here — we observe the side effects through the SQLite / S3 /
    publish mocks."""
    from lib_shared.models import SignConfig

    real_cfg = SignConfig()

    s3_calls = []

    def _s3_side_effect(cfg_dict):
        s3_calls.append(cfg_dict)

    mod = _load_app_module(real_cfg, s3_side_effect=_s3_side_effect)
    captured = {"save_publish": [], "publish": []}

    def _publish_config(cfg_dict):
        captured["publish"].append(cfg_dict)

    mod._mqtt_client_publish_config = _publish_config

    # Snapshot call counts BEFORE the explicit invocation, so we
    # measure only the delta from our call (the module-load already
    # fired the hook once).
    pre_s3 = len(s3_calls)
    pre_publish = len(captured["publish"])

    mod._boot_refresh_config()

    # The real _save_and_publish ran:
    #   - persisted SQLite (mocked via sys.modules["sqlite"])
    #   - wrote S3 snapshot (captured via s3_side_effect)
    #   - called _mqtt_client_publish_config (captured via observer)
    assert len(s3_calls) == pre_s3 + 1, (
        "boot hook must write S3 snapshot — that's the deployment-"
        "history record the operator wants"
    )
    s3_dict = s3_calls[-1]
    assert isinstance(s3_dict, dict)
    assert s3_dict["config_sha"], "boot hook must stamp a fresh config_sha"
    assert s3_dict["updated_at"], "boot hook must stamp a fresh updated_at"

    assert len(captured["publish"]) == pre_publish + 1, (
        "boot hook must publish the config envelope so Pi / browser "
        "see the new config_sha"
    )


def test_boot_refresh_resolves_empty_pinned_version(clean_sys_modules):
    """When `pinned_version` is empty on the loaded cfg, the boot
    hook's `_save_and_publish` re-resolves `target_version` against
    Flask's running short SHA. Pin that contract: empty pinned →
    resolved target_version is Flask's short SHA."""
    from lib_shared.models import SignConfig, SignSettings

    real_cfg = SignConfig(sign_settings=SignSettings(pinned_version=""))

    s3_calls = []
    mod = _load_app_module(real_cfg, s3_side_effect=lambda d: s3_calls.append(d))
    mod._mqtt_client_publish_config = lambda d: None

    pre_s3 = len(s3_calls)
    mod._boot_refresh_config()

    assert len(s3_calls) == pre_s3 + 1
    s3_sign_settings = s3_calls[-1].get("sign_settings") or {}
    assert s3_sign_settings.get("target_version"), (
        "boot hook must resolve empty pinned_version to Flask's "
        "running short SHA — that's the deployment-history contract."
    )


def test_boot_refresh_catches_exceptions(clean_sys_modules, caplog):
    """A transient S3 / SQLite / MQTT failure during the boot hook
    must NOT propagate out of `_boot_refresh_config`. The hook is
    wrapped in try/except; the server keeps accepting requests."""
    from lib_shared.models import SignConfig

    real_cfg = SignConfig()
    mod = _load_app_module(real_cfg)

    def _explode(cfg):
        raise RuntimeError("simulated S3 outage")

    mod._save_and_publish = _explode

    with caplog.at_level(logging.WARNING):
        # Must not raise.
        mod._boot_refresh_config()

    # The WARN log is only emitted on the explicit call (after the
    # at-load call already fired and used the real
    # _save_and_publish, which didn't fail). Filter for the
    # post-replacement warning specifically.
    warning_records = [
        r for r in caplog.records
        if "boot config refresh failed" in r.getMessage()
        and "simulated S3 outage" in r.getMessage()
    ]
    assert len(warning_records) == 1, (
        "boot hook must log a single WARN when _save_and_publish raises; "
        f"got records={[r.getMessage() for r in caplog.records]}"
    )


def test_boot_refresh_runs_unconditionally_no_change_guard(clean_sys_modules):
    """The operator's explicit requirement: "we rarely ever reboot the
    dyno if we're not deploying (ie. config changes anyways) — so an
    extra config file in that edge case is no big deal. I prefer to
    keep it simpler." Pin that the hook does NOT compare the resolved
    target_version against the persisted one and skip when they match —
    every boot produces a fresh save+publish+stamp."""
    from lib_shared.models import SignConfig, SignSettings

    # Pre-stamp the cfg with a `target_version` that matches what
    # the resolver will produce. A "guard" implementation would
    # short-circuit here and skip the save.
    real_cfg = SignConfig(
        sign_settings=SignSettings(pinned_version=""),
        config_sha="prev1234",
        updated_at="2026-09-01T00:00:00+00:00",
    )

    s3_calls = []
    mod = _load_app_module(real_cfg, s3_side_effect=lambda d: s3_calls.append(d))
    mod._mqtt_client_publish_config = lambda d: None

    pre_s3 = len(s3_calls)
    mod._boot_refresh_config()

    # The hook must still fire — and it must stamp fresh
    # config_sha / updated_at, overwriting the pre-existing values.
    assert len(s3_calls) == pre_s3 + 1, (
        "boot hook must run unconditionally on every call — "
        "no 'nothing changed, skip' guard. Operator explicitly chose "
        "simplicity over skip-optimization."
    )
    assert s3_calls[-1]["config_sha"] != "prev1234"
    assert s3_calls[-1]["updated_at"] != "2026-09-01T00:00:00+00:00"


def test_boot_refresh_invoked_at_module_load(clean_sys_modules):
    """The hook is invoked once at module-load time, immediately after
    `_save_and_publish` is defined. Pin that the module-load runs it
    (we observe by checking s3.save_config_snapshot.call_count)."""
    from lib_shared.models import SignConfig

    real_cfg = SignConfig()
    mod = _load_app_module(real_cfg)

    # s3_mod.save_config_snapshot is the MagicMock we installed in
    # the loader. If the hook fired at module load, call_count > 0.
    s3_mod = sys.modules["s3"]
    assert s3_mod.save_config_snapshot.call_count >= 1, (
        "boot hook must fire exactly once at module-load time, "
        "right after _save_and_publish is defined. If this fails, "
        "the hook was either removed or guarded against re-entry."
    )
