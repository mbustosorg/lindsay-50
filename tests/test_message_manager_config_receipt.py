"""Tests for issue #71: MessageManager._last_applied_config_sha +
get_last_config_receipt().

A config envelope stamped with ``config_sha`` lands via
``_handle_config``. The MessageManager captures the SHA into a
local attribute (``self._last_applied_config_sha``) so the Pi status
snapshot AND the browser's `App.getLastConfigReceipt()` can read it.

The receipt shape is just ``{"sha": str}`` — no timestamp, by
design (the user explicitly rejected echoing ``updated_at`` or
chasing the broker tick rate).
"""

from __future__ import annotations

from lib_shared.models import SignConfig


def _make_mm():
    """Build a fresh MessageManager. Constructor needs three URLs,
    which we never use in this test file — _handle_config dispatches
    in-process."""
    from lib_shared.message_manager import MessageManager

    return MessageManager(
        messages_api_url="http://test/api/messages",
        config_api_url="http://test/api/config",
        api_key="test-key",
    )


def _envelope(cfg: SignConfig) -> dict:
    """Build a config-payload dict — the shape `_handle_config`
    expects (the broker envelope is unwrapped before dispatch)."""
    return cfg.to_dict()


def test_get_last_config_receipt_returns_empty_on_fresh_install():
    """No config envelope has arrived yet — both
    _last_applied_config_sha and the wire-stamped config_sha are
    empty. Receipt is {"sha": ""} so JS can render "—" cold-start."""
    mm = _make_mm()
    receipt = mm.get_last_config_receipt()
    assert receipt == {"sha": ""}


def test_handle_config_captures_stamped_sha():
    """After a config envelope lands, the SHA stamped by Flask
    round-trips into _last_applied_config_sha and shows up in the
    receipt."""
    mm = _make_mm()
    cfg = SignConfig(config_sha="abc1234", updated_at="2026-09-12T10:00:00+00:00")
    mm._handle_config(_envelope(cfg))
    assert mm._last_applied_config_sha == "abc1234"
    assert mm.get_last_config_receipt() == {"sha": "abc1234"}


def test_handle_config_updates_receipt_on_each_envelope():
    """Two envelopes in sequence: receipt tracks the most-recent."""
    mm = _make_mm()
    mm._handle_config(_envelope(SignConfig(config_sha="1111111")))
    assert mm.get_last_config_receipt() == {"sha": "1111111"}
    mm._handle_config(_envelope(SignConfig(config_sha="2222222")))
    assert mm.get_last_config_receipt() == {"sha": "2222222"}


def test_handle_config_with_unstamped_payload_falls_back():
    """A config envelope WITHOUT a stamped config_sha (older
    payload, or a save that bypassed _save_and_publish) leaves
    _last_applied_config_sha empty; receipt falls back to the
    wire-stamped config_sha on self._config, which is also empty."""
    mm = _make_mm()
    mm._handle_config(_envelope(SignConfig()))  # no config_sha
    assert mm._last_applied_config_sha == ""
    receipt = mm.get_last_config_receipt()
    # Falls back to self._config.config_sha, which is also empty.
    assert receipt == {"sha": ""}


def test_get_last_config_receipt_falls_back_to_config_sha():
    """If _handle_config never ran but the in-memory SignConfig was
    seeded from REST with a stamped config_sha, the receipt reads
    that value."""
    mm = _make_mm()
    mm._config = SignConfig(config_sha="seeded12", updated_at="2026-09-12T10:00:00+00:00")
    assert mm._last_applied_config_sha == ""  # never set
    receipt = mm.get_last_config_receipt()
    assert receipt == {"sha": "seeded12"}


def test_receipt_takes_recent_over_seeded():
    """Once a config envelope arrives, the receipt switches from
    the seeded SHA to the freshly-applied SHA — even if they differ
    (which they shouldn't, but the receipt must reflect the latest
    observed truth)."""
    mm = _make_mm()
    mm._config = SignConfig(config_sha="seeded12")
    mm._handle_config(_envelope(SignConfig(config_sha="applied")))
    assert mm.get_last_config_receipt() == {"sha": "applied"}


def test_receipt_returns_plain_dict_not_live_ref():
    """The receipt is a fresh dict on each call — callers can't
    mutate the MessageManager's internal state by holding a
    reference to the returned dict."""
    mm = _make_mm()
    mm._handle_config(_envelope(SignConfig(config_sha="abc1234")))
    receipt_a = mm.get_last_config_receipt()
    receipt_a["sha"] = "MUTATED"
    receipt_b = mm.get_last_config_receipt()
    assert receipt_b["sha"] == "abc1234"
