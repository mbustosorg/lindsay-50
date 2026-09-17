"""Tests for `_wire_sign_settings` — issue #71 wire-form target_version.

The settings input lets operators leave `sign_settings.target_version`
empty (meaning "inherit Flask's running version"). The on-disk
SignConfig and SQLite row keep that raw empty value so the /settings
page can render "inheriting" in the placeholder.

But on the WIRE (MQTT envelope, /api/config GET response, S3
snapshot) the resolved value lands verbatim — observers (Pi, browser
preview, the dashboard's "Current Config" modal) see what's actually
desired without having to interpret empty-vs-pinned semantics.

`_wire_sign_settings(cfg_dict)` is a pure dict-rewriter that:

  1. Returns the input unchanged when target_version is non-empty
     (operator-pinned — common case, zero allocation overhead).
  2. Returns the input unchanged when target_version is empty AND
     Flask's running SHA can't be resolved (very rare — env-only
     deployment with no git remote and no HEROKU_SLUG_COMMIT).
  3. Returns a NEW shallow-copied tree (cfg_dict + sign_settings)
     with `target_version = flask_short_sha` when target_version is
     empty AND Flask's running SHA resolves successfully.

The helper lives in `heart-message_manager/main.py`, which pulls in
the entire Flask app (sqlite, s3, MQTT, etc.) at module load. We
re-implement the helper inline (1:1 logic) and compare against the
live helper via importlib under a sys.modules harness — the same
pattern used by `tests/test_resolve_flask_config_sha.py`. Drift
between the inline mirror and the live helper is caught by
`test_inline_matches_live`.
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
    return s[:7]


def _wire_inline(cfg_dict: dict, flask_short_sha: str) -> dict:
    """Inline mirror of `_wire_sign_settings` for cross-checking.

    Returns a NEW dict tree when resolution happens; returns the input
    unchanged (same object — not a copy) when no resolution is
    needed. The live helper must agree on both shape and identity.
    """
    ss = cfg_dict.get("sign_settings")
    if not isinstance(ss, dict):
        return cfg_dict
    target = (ss.get("target_version") or "").strip()
    if target:
        return cfg_dict
    if not flask_short_sha:
        return cfg_dict
    return {
        **cfg_dict,
        "sign_settings": {
            **ss,
            "target_version": flask_short_sha,
        },
    }


@pytest.fixture
def live_helper():
    """Import the live `_wire_sign_settings` from
    `heart_message_manager.main` under a sys.modules harness that
    mocks out the heavy imports. Mirrors the test_resolve_flask_config_sha
    pattern (full Flask app is overkill for a dict-rewriter).
    """
    captured = {}

    def _mock(name, **attrs):
        mod = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(mod, k, v)
        sys.modules[name] = mod
        return mod

    lib_shared = _mock("lib_shared")
    lib_shared.__path__ = [str(_PROJECT_ROOT / "lib_shared")]
    _mock("lib_shared.config_reader", get_config=MagicMock())
    _mock("lib_shared.log_setup", configure_logging=MagicMock())
    _mock(
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
    sys.modules["lib_shared.models"].EffectsSettings = es
    _mock(
        "lib_shared.config_migrations",
        migrate=MagicMock(side_effect=lambda d, current_version: d or {}),
        migrate_on_startup=MagicMock(),
    )
    _mock("lib_shared.message_manager", MessageManager=MagicMock())

    _mock("heart_message-manager.sqlite")
    _mock("heart_message-manager.s3")
    _mock("heart_message-manager.server_time")
    _mock("heart-message-manager.paho_mqtt_client")
    _mock(
        "sqlite",
        rebuild_from_s3=MagicMock(),
        get_config=MagicMock(),
        get_all_messages=MagicMock(return_value=[]),
        get_messages_since=MagicMock(return_value=[]),
        message_count=MagicMock(return_value=0),
        put_message=MagicMock(),
        get_message=MagicMock(return_value=None),
        put_config=MagicMock(),
    )
    _mock(
        "s3",
        load_messages_from_s3=MagicMock(return_value=[]),
        load_latest_config=MagicMock(return_value=None),
        log_message=MagicMock(),
        save_config_snapshot=MagicMock(),
        _s3_bucket=MagicMock(return_value="test-bucket"),
        _s3_client=MagicMock(),
    )
    _mock("server_time", format_from_iso=lambda *a, **k: "", now_utc_iso=lambda: "2026-05-22T00:00:00Z")
    _mock("paho_mqtt_client", PahoMqttClient=MagicMock())

    auth_path = _PROJECT_ROOT / "heart-message-manager" / "auth.py"
    auth_spec = importlib.util.spec_from_file_location(
        "heart_message_manager_auth", str(auth_path)
    )
    auth_mod = importlib.util.module_from_spec(auth_spec)
    sys.modules["heart-message-manager.auth"] = auth_mod
    sys.modules["auth"] = auth_mod  # main.py uses bare `import auth`
    auth_spec.loader.exec_module(auth_mod)

    main_path = _PROJECT_ROOT / "heart-message-manager" / "main.py"
    main_spec = importlib.util.spec_from_file_location(
        "heart_message_manager_main_test", str(main_path)
    )
    main_mod = importlib.util.module_from_spec(main_spec)
    sys.modules["heart-message-manager.main"] = main_mod
    main_spec.loader.exec_module(main_mod)

    captured["main"] = main_mod
    return captured["main"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestWireSignSettingsResolve:
    def test_resolves_empty_target_to_flask_sha(self, live_helper, monkeypatch):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="86537d5"),
        )
        out = live_helper._wire_sign_settings({
            "sign_settings": {"target_version": "", "sign_name": "Heart"},
            "filters": [],
        })
        assert out["sign_settings"]["target_version"] == "86537d5"
        assert out["sign_settings"]["sign_name"] == "Heart"
        # Other top-level keys preserved.
        assert out["filters"] == []

    def test_resolves_whitespace_only_target(self, live_helper, monkeypatch):
        """`target_version = "   "` is operator-empty by stripping —
        the wire form resolves it just like `""`."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="abc1234"),
        )
        out = live_helper._wire_sign_settings({
            "sign_settings": {"target_version": "   ", "sign_name": "Heart"},
        })
        assert out["sign_settings"]["target_version"] == "abc1234"

    def test_leaves_operator_pinned_target_alone(self, live_helper, monkeypatch):
        """Operator-pinned non-empty target_version is returned
        VERBATIM (truncation is the operator's choice — pass what
        they typed; the Pi tolerates any string)."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="never-used"),
        )
        cfg_dict = {
            "sign_settings": {
                "target_version": "abc1234",
                "sign_name": "Heart",
            },
        }
        out = live_helper._wire_sign_settings(cfg_dict)
        # Same object — no allocation for the hot path.
        assert out is cfg_dict
        assert out["sign_settings"]["target_version"] == "abc1234"

    def test_returns_raw_when_flask_sha_unresolvable(self, live_helper, monkeypatch):
        """If Flask cannot resolve its own running SHA (no
        HEROKU_SLUG_COMMIT, no git, no remote), the wire form keeps
        the raw empty target_version. Better to send "" than a
        wrong SHA — the Pi treats empty as 'inherit' on its own."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha=""),
        )
        cfg_dict = {
            "sign_settings": {"target_version": "", "sign_name": "Heart"},
        }
        out = live_helper._wire_sign_settings(cfg_dict)
        # Same object — no allocation when we can't help.
        assert out is cfg_dict
        assert out["sign_settings"]["target_version"] == ""

    def test_does_not_mutate_input_dict(self, live_helper, monkeypatch):
        """Returns a NEW tree when resolving; the caller's input
        must be untouched (SignConfig.to_dict() may have multiple
        readers in the same request — Settings POST + /api/config
        GET in the same batch both call _wire_sign_settings on the
        same dict tree)."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="deadbee"),
        )
        ss = {"target_version": "", "sign_name": "Heart"}
        cfg_dict = {"sign_settings": ss, "filters": []}
        out = live_helper._wire_sign_settings(cfg_dict)
        # Input unchanged.
        assert cfg_dict["sign_settings"] is ss
        assert ss["target_version"] == ""
        # Output is a new tree.
        assert out is not cfg_dict
        assert out["sign_settings"] is not ss
        assert out["sign_settings"]["target_version"] == "deadbee"

    def test_handles_missing_sign_settings(self, live_helper, monkeypatch):
        """A pre-v3 SignConfig with no `sign_settings` key (or a
        non-dict value at that key) passes through unchanged."""
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha="never"),
        )
        # Missing sign_settings key entirely.
        out = live_helper._wire_sign_settings({"filters": []})
        assert out == {"filters": []}
        # sign_settings present but not a dict (legacy test fixture shape).
        out2 = live_helper._wire_sign_settings({"sign_settings": "broken"})
        assert out2 == {"sign_settings": "broken"}


class TestInlineMatchesLive:
    """The inline `_wire_inline` mirror is the test contract. If it
    drifts from the live helper, the dashboard's "Current Config"
    modal will start showing a different value than this test suite
    expects — exactly the bug class issue #71 is meant to prevent.
    """

    @pytest.mark.parametrize("flask_sha", ["", "86537d5", "abc1234567"])
    def test_inline_matches_live(self, live_helper, monkeypatch, flask_sha):
        monkeypatch.setattr(
            live_helper, "_resolve_boot_config",
            lambda: SimpleNamespace(short_sha=flask_sha),
        )
        cases = [
            {"sign_settings": {"target_version": ""}, "filters": []},
            {"sign_settings": {"target_version": "explicit-pin"}},
            {"sign_settings": {"target_version": "   "}, "x": 1},
            {"sign_settings": {"target_version": "", "sign_name": "Heart"}},
            {"filters": []},  # no sign_settings key
            {"sign_settings": "broken"},  # non-dict value
        ]
        for cfg_dict in cases:
            live = live_helper._wire_sign_settings(cfg_dict)
            inline = _wire_inline(cfg_dict, flask_sha)
            assert live == inline, (
                f"drift: live={live!r} inline={inline!r} "
                f"flask_sha={flask_sha!r}"
            )


class TestApiGetConfigAppliesResolve:
    """The /api/config GET endpoint applies `_wire_sign_settings`
    inline (`jsonify(_wire_sign_settings(cfg.to_dict()))`). The
    route-level integration test would need full Flask auth +
    request context scaffolding (api_login_required chain) which
    is overkill for a 1-line wire rewriter. The end-to-end
    relationship is documented by the call site in main.py
    `api_get_config`; the helper itself is exercised in the tests
    above."""

    def test_call_site_in_api_get_config(self):
        """The /api/config route handler should call
        `_wire_sign_settings(cfg.to_dict())` before jsonifying. If
        someone removes that wiring, this test fails — guarding
        against accidental helper-detachment during refactors."""
        from pathlib import Path

        main_src = (
            Path(__file__).parent.parent / "heart-message-manager" / "main.py"
        ).read_text()
        assert "_wire_sign_settings(cfg.to_dict())" in main_src, (
            "/api/config GET must apply _wire_sign_settings to cfg.to_dict()"
        )


# ---------------------------------------------------------------------------
# Round 11 (issue #71 follow-up): S3 snapshot must NOT receive the resolved
# wire form. If it does, an empty `target_version` (operator's "inherit
# Flask version" intent) gets permanently resolved to a concrete Flask SHA
# on the next save — and the next Flask restart loads that SHA back from
# S3 into SQLite, silently converting "inherit" into a hard pin.
# ---------------------------------------------------------------------------


def test_save_and_publish_s3_snapshot_uses_resolved_dict():
    """`_save_and_publish` MUST call `s3.save_config_snapshot` with the
    SignConfig dict that carries BOTH `pinned_version` (operator's raw
    input) and `target_version` (the resolved concrete value).

    Round 11 (issue #71 follow-up): the raw-vs-wire divergence is
    ELIMINATED. The on-disk SignConfig, the S3 snapshot, and the
    MQTT envelope now carry the same JSON object — both
    `pinned_version` AND `target_version` are always present. The
    resolver in `_save_and_publish` writes the resolved value to
    `cfg.sign_settings.target_version` BEFORE serialization, so
    `cfg.to_dict()` is the SINGLE source of truth.

    Tests the round-11 invariant: `cfg_dict` (renamed from
    `cfg_raw`) is the arg passed to `s3.save_config_snapshot`.
    """
    from pathlib import Path

    main_src = (
        Path(__file__).parent.parent / "heart-message-manager" / "main.py"
    ).read_text()
    fn_idx = main_src.find("def _save_and_publish")
    assert fn_idx != -1, "_save_and_publish must be defined"
    fn_section = main_src[fn_idx : fn_idx + 5000]
    s3_call_idx = fn_section.find("s3.save_config_snapshot(")
    assert s3_call_idx != -1, "s3.save_config_snapshot call must be in _save_and_publish"
    s3_call_segment = fn_section[s3_call_idx : s3_call_idx + 200]
    # Round 11 contract: S3 receives cfg_dict (the resolved dict
    # carrying BOTH pinned_version and target_version), NOT
    # cfg_wire (the legacy wire-resolved form).
    assert (
        "cfg_dict" in s3_call_segment
    ), (
        "s3.save_config_snapshot must receive cfg_dict (the resolved "
        "SignConfig dict with both pinned_version and target_version), "
        "not cfg_wire — the raw-vs-wire divergence was eliminated in "
        "round 11 (issue #71 follow-up)."
    )


def test_save_and_publish_mqtt_publish_uses_wire_form():
    """`_save_and_publish` MUST call `_mqtt_client_publish_config`
    with the wire-form dict (post-`_wire_sign_settings`).

    The wire form is now structurally identical to the disk form
    (both carry `pinned_version` AND `target_version`). The
    `_wire_sign_settings` step is retained for back-compat with
    legacy pre-r11 callers that may have called it directly — the
    round-11 path invokes it as the FINAL step before publish so
    any legacy caller's resolver logic still runs.
    """
    from pathlib import Path

    main_src = (
        Path(__file__).parent.parent / "heart-message-manager" / "main.py"
    ).read_text()
    fn_idx = main_src.find("def _save_and_publish")
    assert fn_idx != -1, "_save_and_publish must be defined"
    # The MQTT call can land deep in the function body; widen the
    # window so we capture both the resolver and the call site.
    fn_section = main_src[fn_idx : fn_idx + 15000]
    mqtt_call_idx = fn_section.find("_mqtt_client_publish_config(")
    assert mqtt_call_idx != -1, "_mqtt_client_publish_config call must be in _save_and_publish"
    mqtt_call_segment = fn_section[mqtt_call_idx : mqtt_call_idx + 200]
    assert (
        "cfg_wire" in mqtt_call_segment
    ), (
        "_mqtt_client_publish_config must receive cfg_wire (post-`_wire_sign_settings` "
        "form) — keeps the wire resolver in the publish path so legacy callers "
        "still see consistent shape."
    )
