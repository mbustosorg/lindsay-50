"""Tests for `_resolve_flask_config_sha` — issue #71 dashboard fallback.

The Versions card's "Flask / Config" cell renders the
per-save content hash stamped by `_save_and_publish`. Before the
operator's first /settings save after v186 deploys, that field is
empty in SQLite — the cell would render "—" forever. The helper
falls through three layers so the cell always shows a meaningful
value:

1. The most-recent per-save config_sha (preferred — actual save).
2. The operator-pinned sign_settings.target_version, truncated to
   7 chars (matches what /api/sign/settings returns on the wire).
3. Flask's own running short SHA — what the Pi targets by default.

Returns "" only when ALL THREE layers are empty (template renders "—").

The helper lives in `heart_message_manager/main.py`, which pulls
in the entire Flask app (sqlite, s3, MQTT, etc.) at module load.
Re-importing that module in a test requires the same heavy mock
scaffolding as test_auth / test_sign_status_endpoint, which is
overkill for a 3-line pure function. Instead, this test pins the
helper's contract by re-implementing it inline (with the same
logic) and asserting against the live `heart_message_manager.main`
helper via importlib under a sys.modules harness that mirrors
test_sign_status_endpoint's approach.

Both implementations are kept in lockstep by direct comparison
in `test_helper_matches_inline_implementation`.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


_PROJECT_ROOT = Path(__file__).parent.parent


def _short_sha(s: str) -> str:
    """Identical to ``lib_shared.boot_config.short_sha`` — first 7 chars."""
    return s[:7]


def _resolve_inline(cfg, boot_short_sha: str) -> str:
    """Inline mirror of the live helper for cross-checking.

    Kept small on purpose so any future drift between this and the
    live helper is caught by `test_helper_matches_inline_implementation`.
    """
    saved = getattr(cfg, "config_sha", "") or ""
    if saved:
        return saved
    target = (
        (cfg.sign_settings.target_version or "")
        if cfg.sign_settings
        else ""
    )
    if target:
        return _short_sha(target) or ""
    return boot_short_sha or ""


def _cfg(config_sha: str = "", target_version: str = "", sign_settings=None):
    if sign_settings is None and (target_version or config_sha):
        sign_settings = SimpleNamespace(target_version=target_version)
    return SimpleNamespace(
        config_sha=config_sha,
        sign_settings=sign_settings,
    )


@pytest.fixture
def live_helper():
    """Import the live helper from heart_message_manager.main under
    a sys.modules harness that mocks out the heavy imports. We don't
    exercise the rest of main.py — just the helper function the
    dashboard route calls."""
    captured: dict = {}

    # Mock all the heavy deps that main.py pulls in at import time.
    def _mock(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    # lib_shared.* namespace + config_reader + log_setup + models + migrations
    lib_shared = _mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    cr_mod = _mock("lib_shared.config_reader", get_config=MagicMock())
    ls_mod = _mock("lib_shared.log_setup", configure_logging=MagicMock())
    models_mod = _mock(
        "lib_shared.models",
        SignConfig=MagicMock(),
        FilterRule=MagicMock(),
        Message=MagicMock(),
        TextSettings=MagicMock(),
        EffectsSettings=MagicMock(),
        MessageEnvelope=MagicMock(),
        MessageView=MagicMock(),
    )
    es = MagicMock()
    es.MIN_LOOKBACK_DAYS = 1
    es.MAX_LOOKBACK_DAYS = 365
    es.VALID_SELECTOR_ALGORITHMS = ("weighted", "random")
    models_mod.EffectsSettings = es
    cm_mod = _mock(
        "lib_shared.config_migrations",
        migrate=MagicMock(side_effect=lambda d, current_version: d or {}),
        migrate_on_startup=MagicMock(),
    )
    mm_mod = _mock("lib_shared.message_manager", MessageManager=MagicMock())

    # Heavy heart_message_manager deps
    _mock("heart_message-manager.sqlite")
    _mock("heart_message-manager.s3")
    _mock("heart_message-manager.server_time")
    _mock("heart-message-manager.paho_mqtt_client")
    _mock("sqlite", rebuild_from_s3=MagicMock(), get_config=MagicMock(),
          get_all_messages=MagicMock(return_value=[]), get_messages_since=MagicMock(return_value=[]),
          message_count=MagicMock(return_value=0), put_message=MagicMock(),
          get_message=MagicMock(return_value=None), put_config=MagicMock())
    _mock("s3", load_messages_from_s3=MagicMock(return_value=[]), load_latest_config=MagicMock(return_value=None),
          log_message=MagicMock(), save_config_snapshot=MagicMock(),
          _s3_bucket=MagicMock(return_value="test-bucket"), _s3_client=MagicMock())
    _mock("server_time", format_from_iso=lambda *a, **k: "", now_utc_iso=lambda: "2026-05-22T00:00:00Z")
    _mock("paho_mqtt_client", PahoMqttClient=MagicMock())

    # auth.py (real) — needed because main.py imports it.
    auth_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_spec = importlib.util.spec_from_file_location("heart_message_manager_auth", str(auth_path))
    auth_mod = importlib.util.module_from_spec(auth_spec)
    sys.modules["heart-message-manager.auth"] = auth_mod
    sys.modules["auth"] = auth_mod  # main.py uses bare `import auth`
    auth_spec.loader.exec_module(auth_mod)

    # main.py itself
    main_path = _PROJECT_ROOT / "heart-message-manager" / "main.py"
    main_spec = importlib.util.spec_from_file_location("heart_message_manager_main_test", str(main_path))
    main_mod = importlib.util.module_from_spec(main_spec)
    sys.modules["heart-message-manager.main"] = main_mod
    main_spec.loader.exec_module(main_mod)

    captured["main"] = main_mod
    return captured["main"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveFlaskConfigSha:
    def test_uses_per_save_config_sha_when_present(self, live_helper):
        cfg = _cfg(config_sha="abc1234", target_version="never-used")
        assert live_helper._resolve_flask_config_sha(cfg) == "abc1234"

    def test_falls_through_to_target_version_when_config_sha_empty(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="unused"),
        )
        cfg = _cfg(config_sha="", target_version="1234567890abcdef")
        assert live_helper._resolve_flask_config_sha(cfg) == "1234567"

    def test_falls_through_to_target_version_under_7_chars(self, live_helper, monkeypatch):
        """A target_version shorter than 7 chars (rare but possible if
        an operator typed a partial SHA) is truncated — short_sha is
        defined as `input[:7]`, so a 4-char string round-trips as
        itself (no padding)."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="unused"),
        )
        cfg = _cfg(target_version="abc")
        assert live_helper._resolve_flask_config_sha(cfg) == "abc"

    def test_falls_through_to_boot_config_when_no_target_version(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="deadbee"),
        )
        cfg = _cfg(config_sha="", target_version="")
        assert live_helper._resolve_flask_config_sha(cfg) == "deadbee"

    def test_returns_empty_when_all_layers_empty(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha=""),
        )
        cfg = _cfg(config_sha="", target_version="")
        assert live_helper._resolve_flask_config_sha(cfg) == ""

    def test_handles_missing_sign_settings(self, live_helper, monkeypatch):
        """Pre-v3 SignConfig stored timezone at the top level, not in
        sign_settings. The helper must tolerate sign_settings=None
        rather than raising AttributeError."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="cafe123"),
        )
        cfg = SimpleNamespace(config_sha="", sign_settings=None)
        assert live_helper._resolve_flask_config_sha(cfg) == "cafe123"

    def test_per_save_sha_takes_priority_over_target_version(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="never"),
        )
        cfg = _cfg(config_sha="saved123", target_version="override4")
        assert live_helper._resolve_flask_config_sha(cfg) == "saved123"

    def test_target_version_takes_priority_over_boot_config(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="default1"),
        )
        cfg = _cfg(config_sha="", target_version="1234567890abcdef")
        assert live_helper._resolve_flask_config_sha(cfg) == "1234567"
        assert live_helper._resolve_flask_config_sha(cfg) != "default1"


class TestHelperMatchesInlineImplementation:
    """The inline ``_resolve_inline`` mirror is the test contract.
    If it ever drifts from the live helper, the dashboard cell will
    start showing a different value than this test suite expects —
    which is exactly the bug class issue #71 is meant to prevent.
    """
    @pytest.mark.parametrize("boot_sha", ["", "abc1234", "1234567890abcdef"])
    def test_inline_matches_live(self, live_helper, monkeypatch, boot_sha):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha=boot_sha),
        )
        cases = [
            _cfg(config_sha="abc1234", target_version="1234567890abcdef"),
            _cfg(config_sha="", target_version="1234567890abcdef"),
            _cfg(config_sha="", target_version=""),
            _cfg(config_sha="saved12", target_version=""),
            SimpleNamespace(config_sha="", sign_settings=None),
            _cfg(config_sha="", target_version="abc"),
        ]
        for cfg in cases:
            live = live_helper._resolve_flask_config_sha(cfg)
            inline = _resolve_inline(cfg, boot_sha)
            assert live == inline, (
                f"drift: live={live!r} inline={inline!r} cfg={cfg!r} boot={boot_sha!r}"
            )
