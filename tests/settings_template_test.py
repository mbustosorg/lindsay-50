"""Tests for the /settings template's Senders + Filter Rules sections (task 6.3).

Renders the real ``settings.html`` through the Flask app (heavy deps mocked)
and asserts the v3 wire-shape UI: a "Senders" section iterating
``cfg.senders.items()`` with a per-row Action dropdown + Status checkbox, a
normalized phone display, and a Filter Rules table with a per-row Status
checkbox and no ``sender`` option in the Add Rule dropdown.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from settings_flask_harness import login, settings_app


def _make_cfg(senders, filters):
    """A SignConfig-like MagicMock with real senders dict + filters list."""
    cfg = MagicMock()
    cfg.sign.name = "Lindsay's Heart"
    cfg.timezone = "America/Los_Angeles"
    cfg.senders = senders
    cfg.filters = filters
    cfg.text_settings.speed = 3
    cfg.text_settings.color = 0xFF0000
    cfg.text_settings.text_effect = "scroll"
    # None on every timing field → the template falls back to the loader value.
    cfg.effects_settings.fade_seconds = None
    cfg.effects_settings.hold_seconds = None
    cfg.effects_settings.intro_seconds = None
    cfg.effects_settings.idle_seconds = None
    cfg.effects_settings.recent_count = None
    cfg.effects_settings.effects = None
    return cfg


def _render_settings(monkeypatch, cfg):
    with settings_app(monkeypatch) as (flask_app, sqlite_mod):
        sqlite_mod.get_config.return_value = cfg
        client = flask_app.test_client()
        login(client)
        resp = client.get("/settings")
        assert resp.status_code == 200, resp.data
        return resp.get_data(as_text=True)


@pytest.fixture
def two_senders():
    # Alice: allow + enabled, operator typed a formatted phone (dict key is
    # the normalized form). Bob: suppress + disabled.
    return {
        "+15551234567": {
            "name": "Alice",
            "action": "allow",
            "status": "enabled",
            "phone": "+1 (555) 123-4567",
        },
        "+15558888888": {
            "name": "Bob",
            "action": "suppress",
            "status": "disabled",
            "phone": "+15558888888",
        },
    }


@pytest.fixture
def two_filters():
    return [
        types.SimpleNamespace(type="keyword", pattern="spam", action="suppress", status="enabled"),
        types.SimpleNamespace(type="regex", pattern="^x$", action="suppress", status="disabled"),
    ]


def test_no_allowed_senders_reference(monkeypatch, two_senders, two_filters):
    """The broken cfg.allowed_senders iteration is fully gone."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert "allowed_senders" not in body


def test_section_title_is_senders(monkeypatch, two_senders, two_filters):
    """The section header is 'Senders', not 'Allowed Senders'."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert "Senders" in body
    assert "Allowed Senders" not in body


def test_normalization_helper_line(monkeypatch, two_senders, two_filters):
    """A helper line explains phone normalization."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert "Phone numbers are normalized to +1XXXXXXXXXX." in body


def test_senders_status_is_checkbox(monkeypatch, two_senders, two_filters):
    """The Status column renders a checkbox named sender_status, indexed by row."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    # Row 0 (Alice, enabled) checkbox checked; row 1 (Bob, disabled) unchecked.
    assert 'name="sender_status" value="0" checked' in body
    assert 'name="sender_status" value="1" class=' in body


def test_action_dropdown_selected_matches_entry(monkeypatch, two_senders, two_filters):
    """Each row's Action dropdown reflects the entry's action."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert '<option value="allow" selected>Allow</option>' in body
    assert '<option value="suppress" selected>Suppress</option>' in body


def test_phone_input_shows_normalized_key(monkeypatch, two_senders, two_filters):
    """The Phone field shows the normalized dict key, not the original input."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert 'value="+15551234567"' in body
    # Alice's original formatted phone must NOT be rendered as the input value.
    assert "+1 (555) 123-4567" not in body


def test_filter_rules_status_checkbox(monkeypatch, two_senders, two_filters):
    """The Filter Rules table renders a per-row Status checkbox (not a dropdown)."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert 'name="filter_status_0" form="settings-form" checked' in body
    assert 'name="filter_status_1" form="settings-form" class=' in body


def test_add_rule_dropdown_excludes_sender(monkeypatch, two_senders, two_filters):
    """The Add Rule Type dropdown offers keyword/regex/message only."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert '<option value="keyword">keyword</option>' in body
    assert '<option value="regex">regex</option>' in body
    assert '<option value="message">message</option>' in body
    assert '<option value="sender">' not in body


def test_add_rule_has_enabled_checkbox(monkeypatch, two_senders, two_filters):
    """The Add Rule form includes a filter_status checkbox defaulting to checked."""
    body = _render_settings(monkeypatch, _make_cfg(two_senders, two_filters))
    assert 'name="filter_status" checked' in body
