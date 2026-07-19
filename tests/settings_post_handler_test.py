"""Tests for the /settings POST handler's senders + filter-status parsing (task 5.3).

Drives the real ``settings()`` handler through the Flask app (heavy deps
mocked) and asserts the rebuilt ``cfg.senders`` dict-of-dict shape: per-row
Action dropdown, per-row Status checkbox list (indexed by row), phone
normalization with original-preservation, empty-row dropping, and the
zero-rows preservation guard.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest
from werkzeug.datastructures import MultiDict

from settings_flask_harness import login, settings_app


def _make_cfg(initial_senders=None, filters=None):
    """SignConfig-like MagicMock the /settings handler mutates in place."""
    cfg = MagicMock()
    cfg.senders = dict(initial_senders or {})
    cfg.filters = list(filters or [])
    # _save_and_publish indexes cfg.to_dict() as a real dict.
    cfg.to_dict.return_value = {
        "effects_settings": {
            "effects": [{"name": "Fireworks", "enabled": True}],
            "fade_seconds": 2.0,
            "hold_seconds": 15.0,
        },
        "text_settings": {"speed": 3, "color": 0xFF0000},
    }
    return cfg


def _post(monkeypatch, cfg, form):
    """POST `form` to /settings and return the (already-mutated) cfg."""
    with settings_app(monkeypatch) as (flask_app, sqlite_mod):
        sqlite_mod.get_config.return_value = cfg
        client = flask_app.test_client()
        login(client)
        resp = client.post("/settings", data=MultiDict(form))
        assert resp.status_code in (200, 302), resp.data
    return cfg


def test_one_allow_enabled_row(monkeypatch):
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "Alice"),
            ("sender_phone", "+15551234567"),
            ("sender_action", "allow"),
            ("sender_status", "0"),
        ],
    )
    assert cfg.senders["+15551234567"] == {
        "name": "Alice",
        "action": "allow",
        "status": "enabled",
        "phone": "+15551234567",
    }


def test_one_suppress_disabled_row(monkeypatch):
    """A row with action=suppress and no sender_status checkbox → disabled."""
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "Bob"),
            ("sender_phone", "+15558888888"),
            ("sender_action", "suppress"),
        ],
    )
    assert cfg.senders["+15558888888"] == {
        "name": "Bob",
        "action": "suppress",
        "status": "disabled",
        "phone": "+15558888888",
    }


def test_three_rows_independent_checkbox_state(monkeypatch):
    """Rows 0 and 2 checked, row 1 unchecked → enabled/disabled/enabled."""
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "A"),
            ("sender_name", "B"),
            ("sender_name", "C"),
            ("sender_phone", "+15550000001"),
            ("sender_phone", "+15550000002"),
            ("sender_phone", "+15550000003"),
            ("sender_action", "allow"),
            ("sender_action", "allow"),
            ("sender_action", "allow"),
            ("sender_status", "0"),
            ("sender_status", "2"),
        ],
    )
    assert cfg.senders["+15550000001"]["status"] == "enabled"
    assert cfg.senders["+15550000002"]["status"] == "disabled"
    assert cfg.senders["+15550000003"]["status"] == "enabled"


def test_empty_phone_row_dropped(monkeypatch):
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "Test"),
            ("sender_phone", ""),
            ("sender_action", "allow"),
            ("sender_status", "0"),
        ],
    )
    assert cfg.senders == {}


def test_zero_rows_preserves_existing(monkeypatch):
    """A POST with no sender fields at all must not wipe cfg.senders."""
    existing = {
        "+15551234567": {"name": "Alice", "action": "allow", "status": "enabled", "phone": "+15551234567"},
    }
    cfg = _make_cfg(initial_senders=existing)
    _post(monkeypatch, cfg, [("sign_name", "Lindsay's Heart")])
    assert cfg.senders == existing


def test_formatted_phone_normalized_key_original_preserved(monkeypatch):
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "Alice"),
            ("sender_phone", "+1 (555) 123-4567"),
            ("sender_action", "allow"),
            ("sender_status", "0"),
        ],
    )
    assert "+15551234567" in cfg.senders
    assert cfg.senders["+15551234567"]["phone"] == "+1 (555) 123-4567"
    assert cfg.senders["+15551234567"]["status"] == "enabled"


def test_missing_action_defaults_to_allow(monkeypatch):
    cfg = _make_cfg()
    _post(
        monkeypatch,
        cfg,
        [
            ("sender_name", "Alice"),
            ("sender_phone", "+15551234567"),
            ("sender_status", "0"),
        ],
    )
    assert cfg.senders["+15551234567"]["action"] == "allow"


def test_per_row_filter_status_updates(monkeypatch):
    """filter_status_<i> checkboxes update each existing rule's status."""
    filters = [
        types.SimpleNamespace(type="keyword", pattern="spam", action="suppress", status="enabled"),
        types.SimpleNamespace(type="keyword", pattern="ham", action="suppress", status="enabled"),
    ]
    cfg = _make_cfg(filters=filters)
    # Only rule 0's checkbox is checked → rule 0 enabled, rule 1 disabled.
    _post(monkeypatch, cfg, [("filter_status_0", "on")])
    assert cfg.filters[0].status == "enabled"
    assert cfg.filters[1].status == "disabled"
