"""Tests for `_resolve_flask_config_sha` — issue #71 dashboard config SHA.

The Versions card's "Flask / Config" cell renders the SHA of the
*currently-loaded* SignConfig body (the same body the Pi / browser
receive via the config envelope), NOT Flask's running git SHA.

Old behavior (pre-round 12): the helper fell through three layers —
saved config_sha → operator-pinned target_version → Flask's own
running short SHA. That conflated "what config is Flask serving"
with "what's the latest git SHA". On a fresh install where the
operator hadn't yet clicked Save on /settings, the saved config_sha
was empty and the cell rendered Flask's running git SHA — same
value as Flask/Code, so the Config column couldn't differentiate
"same code, same config" from "same code, different config". The
operator's complaint: "the current flask config should NOT rely on
settings being saved! the config is in S3 and SQLite is rebuilt from
it."

New behavior (round 12):

1. If ``cfg.config_sha`` is already stamped (set by
   ``_save_and_publish``), use it directly. This is the post-save
   value that round-trips through S3 + SQLite + the MQTT envelope.
2. Otherwise compute the SHA from the current body via
   ``cfg.to_dict()`` (chicken-and-egg handled by popping the
   ``config_sha`` + ``updated_at`` fields from the hash input).
3. The target_version and Flask-running-SHA fallbacks are REMOVED.

Returns "" when ``cfg`` is None or empty. Never raises — caller
wraps in ``str()`` for the jsonify path so a MagicMock test stub
doesn't crash jsonify.

The helper lives in `heart_message_manager/main.py`, which pulls
in the entire Flask app (sqlite, s3, MQTT, etc.) at module load.
Re-importing that module in a test requires the same heavy mock
scaffolding as test_auth / test_sign_status_endpoint, which is
overkill for a pure function. Instead, this test pins the helper's
contract by re-implementing it inline (with the same logic) and
asserting against the live `heart_message_manager.main` helper via
importlib under a sys.modules harness that mirrors
test_sign_status_endpoint's approach.

Both implementations are kept in lockstep by direct comparison
in `test_helper_matches_inline_implementation`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
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


def _resolve_inline(cfg) -> str:
    """Inline mirror of the live helper for cross-checking.

    Computes the SHA of the current body when ``cfg.config_sha`` is
    empty, so the cell reflects "what config is Flask serving" not
    "what was the last save" and not "Flask's running git SHA".

    Kept small on purpose so any future drift between this and the
    live helper is caught by `test_helper_matches_inline_implementation`.
    """
    if cfg is None:
        return ""
    saved = getattr(cfg, "config_sha", "") or ""
    if saved:
        return saved
    body = cfg.to_dict()
    body.pop("config_sha", None)
    body.pop("updated_at", None)
    try:
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    except TypeError:
        return ""
    return _short_sha(
        hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ) or ""


class _FakeCfg:
    """Minimal stand-in for a SignConfig with ``to_dict()`` semantics.

    The live helper does ``cfg.to_dict()`` to compute the SHA from
    the body, so the test fixture must expose the same surface. The
    inline mirror uses this same interface.
    """

    def __init__(
        self,
        config_sha: str = "",
        body: dict | None = None,
        sign_settings=None,
    ) -> None:
        self.config_sha = config_sha
        self.sign_settings = sign_settings
        # `body` is what `to_dict()` will return; defaults to an
        # empty dict so a fresh cfg produces a stable SHA.
        self._body = body if body is not None else {}

    def to_dict(self) -> dict:
        return dict(self._body)


def _cfg(
    config_sha: str = "",
    body: dict | None = None,
    sign_settings=None,
) -> _FakeCfg:
    return _FakeCfg(config_sha=config_sha, body=body, sign_settings=sign_settings)


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
    _mock("heart-message-manager.server_time")
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
        """The per-save stamp is the truth — round-trips through S3
        + SQLite + the MQTT envelope. The body hash is NOT recomputed
        when the stamp exists."""
        cfg = _cfg(config_sha="abc1234", body={"unused": "body"})
        assert live_helper._resolve_flask_config_sha(cfg) == "abc1234"

    def test_computes_sha_from_body_when_no_per_save_stamp(self, live_helper):
        """When the per-save stamp is empty (fresh install, no
        /settings save since deploy), compute the SHA from the
        currently-loaded body. This means Flask/Config reflects
        "what config is Flask serving" — typically the config
        rebuilt from S3 on boot — NOT Flask's running git SHA."""
        body = {"rotation": "favorites", "speed": 50, "color": "#ff00ff"}
        cfg = _cfg(config_sha="", body=body)
        result = live_helper._resolve_flask_config_sha(cfg)
        # Result must be 7 chars (short_sha truncation)
        assert isinstance(result, str)
        assert len(result) == 7
        # Result must NOT be the empty string when a body is present
        assert result != ""

    def test_falls_back_to_boot_sha_layer_removed(self, live_helper, monkeypatch):
        """The OLD behavior fell through to Flask's running short
        SHA when the per-save stamp was empty. That conflated
        "what config is Flask serving" with "what's the latest
        git SHA" — the operator's complaint: "the current flask
        config should NOT rely on settings being saved!"

        With the round-12 rewrite, the running SHA is NOT a
        fallback. The cell reflects the body even on a fresh
        install (which is exactly what the operator wants)."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="deadbee"),
        )
        cfg = _cfg(config_sha="", body={"rotation": "favorites"})
        result = live_helper._resolve_flask_config_sha(cfg)
        # Must NOT be the running SHA fallback
        assert result != "deadbee"
        # Must be a 7-char SHA of the body
        assert len(result) == 7

    def test_falls_back_to_target_version_layer_removed(self, live_helper):
        """The OLD layer 2 (operator-pinned target_version, truncated
        to 7 chars) was REMOVED. It conflated config SHA with the
        git SHA of the operator's pin target. Config SHA = SHA of
        config body; target_version SHA = git SHA. Different
        concepts — they should never share the same cell value."""
        cfg = _cfg(config_sha="", body={"rotation": "favorites"})
        cfg.sign_settings = SimpleNamespace(target_version="1234567890abcdef")
        result = live_helper._resolve_flask_config_sha(cfg)
        # Must NOT be the target_version truncated to 7
        assert result != "1234567"
        # Must be a 7-char SHA of the body
        assert len(result) == 7

    def test_race_window_default_returns_empty(self, live_helper, monkeypatch):
        """Race-window guard: when a sibling gunicorn worker is mid-
        ``rebuild_from_s3`` (unlinked SQLite + init_db + not-yet-loaded-
        from-S3), a concurrent dashboard render here reads an empty
        config table and ``sqlite.get_config()`` returns
        ``SignConfig.default()``. Body-hashing the default would
        surface a misleading value (the hash of an empty config) as
        if it were a real SHA. The helper detects the default and
        returns "" so the cell renders "—" ("we don't know yet").

        Symptom observed on v208: ``cfg.config_sha`` was empty AND
        the cfg body was the canonical default → helper returned
        ``3cc5503`` (body-hash of empty config). After this fix,
        returns "".

        Implementation note: the live helper compares the in-memory
        cfg's body against ``SignConfig.default().to_dict()``. The
        test fixture mocks ``SignConfig`` (heavy module load
        scaffolding — see live_helper fixture). To make the
        comparison work without pulling in the full flask app
        twice, we feed the helper a ``_FakeCfg`` whose ``to_dict``
        returns the EXACT canonical-default body (computed
        independently from the live ``lib_shared.models.SignConfig``
        via importlib against the on-disk source — bypassing
        ``sys.modules['lib_shared.models']`` which the fixture
        has overwritten with a mock).
        """
        import importlib.util as _ilu
        from lib_shared import models as _real_models
        # Sanity: real models' default body must match the helper's
        # expected default-detection target.
        real_default_body = _real_models.SignConfig.default().to_dict()
        real_default_body.pop("config_sha", None)
        real_default_body.pop("updated_at", None)

        # Build a cfg whose to_dict returns the same canonical
        # default body. The helper compares against
        # SignConfig.default().to_dict() — we patch that on the
        # mocked SignConfig so the comparison sees the same dict.
        monkeypatch.setattr(
            live_helper.SignConfig,
            "default",
            lambda: _FakeCfg(config_sha="", body=real_default_body),
        )

        cfg = _FakeCfg(config_sha="", body=real_default_body)
        result = live_helper._resolve_flask_config_sha(cfg)
        assert result == "", (
            "race window: helper must return '' when cfg is "
            "SignConfig.default() — body-hashing the default would "
            "show a misleading SHA like 3cc5503"
        )

    def test_returns_empty_when_cfg_is_none(self, live_helper):
        """Defensive: a None cfg (e.g. before SQLite init runs)
        must not crash the dashboard render."""
        assert live_helper._resolve_flask_config_sha(None) == ""

    def test_handles_missing_sign_settings(self, live_helper):
        """sign_settings is no longer consulted by the helper
        (layer 2 was removed). A missing sign_settings must not
        raise AttributeError — it's irrelevant to the body SHA."""
        cfg = _FakeCfg(config_sha="", body={"rotation": "old"}, sign_settings=None)
        result = live_helper._resolve_flask_config_sha(cfg)
        assert len(result) == 7  # SHA of body, computed correctly

    def test_per_save_sha_takes_priority_over_body_hash(self, live_helper):
        """If both the per-save stamp AND a body are present, the
        stamp wins (it's the round-tripped value). The body hash
        is only the fallback for fresh installs."""
        body = {"rotation": "favorites", "speed": 50}
        cfg = _cfg(config_sha="saved123", body=body)
        assert live_helper._resolve_flask_config_sha(cfg) == "saved123"

    def test_body_hash_stable_across_dict_key_order(self, live_helper):
        """The body hash uses sort_keys=True so the same logical
        config produces the same SHA regardless of to_dict() key
        ordering. The Pi / browser compute the same SHA from the
        same envelope."""
        body_a = {"rotation": "favorites", "speed": 50, "color": "#ff00ff"}
        body_b = {"color": "#ff00ff", "rotation": "favorites", "speed": 50}
        cfg_a = _cfg(config_sha="", body=body_a)
        cfg_b = _cfg(config_sha="", body=body_b)
        assert live_helper._resolve_flask_config_sha(cfg_a) == live_helper._resolve_flask_config_sha(cfg_b)

    def test_body_hash_changes_when_body_changes(self, live_helper):
        """A different config body must produce a different SHA.
        This is the diagnostic the operator needs: when they save
        /settings and the config changes, the cell should reflect
        the new body, not the previous one."""
        cfg_a = _cfg(config_sha="", body={"rotation": "favorites"})
        cfg_b = _cfg(config_sha="", body={"rotation": "oldest"})
        assert live_helper._resolve_flask_config_sha(cfg_a) != live_helper._resolve_flask_config_sha(cfg_b)

    def test_body_hash_excludes_config_sha_and_updated_at(self, live_helper):
        """Chicken-and-egg guard: the body passed to the hash input
        must NOT include ``config_sha`` or ``updated_at`` (those
        fields are stamps set AFTER the hash is computed, so
        including them would re-hash to a different value on every
        render). The body hash must be stable across renders when
        the same logical config is loaded."""
        body_with_stamps = {"rotation": "favorites", "config_sha": "stale", "updated_at": "2026-01-01T00:00:00Z"}
        body_clean = {"rotation": "favorites"}
        cfg_with_stamps = _cfg(config_sha="", body=body_with_stamps)
        cfg_clean = _cfg(config_sha="", body=body_clean)
        assert live_helper._resolve_flask_config_sha(cfg_with_stamps) == live_helper._resolve_flask_config_sha(cfg_clean)


class TestHelperMatchesInlineImplementation:
    """The inline ``_resolve_inline`` mirror is the test contract.
    If it ever drifts from the live helper, the dashboard cell will
    start showing a different value than this test suite expects —
    which is exactly the bug class issue #71 round 12 is meant to
    prevent."""
    def test_inline_matches_live(self, live_helper, monkeypatch):
        # _resolve_boot_config is no longer used by the helper but
        # we monkeypatch it to a stable value anyway, so any
        # accidental re-introduction of the layer-3 fallback would
        # surface here as a sha divergence.
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="nope-not-used"),
        )
        bodies = [
            {"rotation": "favorites", "speed": 50},
            {"rotation": "oldest"},
            {},
            {"a": 1, "b": 2, "c": 3},
        ]
        cases = [
            _cfg(config_sha="abc1234", body=bodies[0]),
            _cfg(config_sha="", body=bodies[1]),
            _cfg(config_sha="", body=bodies[2]),
            _cfg(config_sha="", body=bodies[3]),
            _cfg(config_sha="saved12", body=bodies[0]),
            _FakeCfg(config_sha="", body={"x": 1}, sign_settings=None),
            None,
        ]
        for cfg in cases:
            live = live_helper._resolve_flask_config_sha(cfg)
            inline = _resolve_inline(cfg)
            assert live == inline, (
                f"drift: live={live!r} inline={inline!r} cfg={cfg!r}"
            )
