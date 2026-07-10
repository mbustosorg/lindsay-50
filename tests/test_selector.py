"""Tests for `lib_shared.selector.MessageSelector` (issue #26).

Covers tasks 6.6-6.13, 6.18, plus 6.15 (renderer+selector integration).
Event-log unit tests live in `test_event_log.py`; the coordinator-level
pre-emption test lives in `test_event_log_integration.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the repo root + heart-matrix-controller to sys.path so the
# `event_log` module is importable from this test.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-matrix-controller"))

from lib_shared.models import Message  # noqa: E402
from lib_shared.selector import (  # noqa: E402
    MessageSelector,
    OFFSET_SECONDS,
    SATURATION_SECONDS,
    USE_WEIGHTED_SELECTOR,
    W_DISPLAY,
    W_FAVORITE,
    W_SEND,
)

# --- helpers ---


def _msg(message_id: str, sent_at_iso: str, body: str = "hello") -> Message:
    """Build a Message with the given id and ISO 8601 received_at."""
    return Message(id=message_id, sender="+15551234567", body=body, received_at=sent_at_iso)


class _FakeEventLog:
    """In-memory fake of the Pi-side `EventLog` (and the browser-side IndexedDBEventLog).

    Exposes the same surface the selector needs (`last_for`, `query`)
    so tests can drive the selector without touching disk or IDB.
    """

    def __init__(self, events: list[dict] | None = None) -> None:
        self._events: list[dict] = list(events or [])

    def append(self, event: dict) -> None:
        self._events.append(event)

    def query(
        self,
        event_type: str | None = None,
        message_id: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        out: list[dict] = []
        for e in self._events:
            if event_type is not None and e.get("event_type") != event_type:
                continue
            if message_id is not None and e.get("message_id") != message_id:
                continue
            if since is not None and float(e.get("timestamp", 0.0)) < since:
                continue
            out.append(e)
        return out

    def last_for(self, message_id: str, event_type: str) -> dict | None:
        latest: dict | None = None
        for e in self._events:
            if e.get("message_id") == message_id and e.get("event_type") == event_type:
                latest = e
        return latest


# --- 6.6 Unit test: display_recency returns 1.0 for a message with no matching event ---


def test_display_recency_is_one_for_never_shown_message():
    """6.6: a message with no matching event has display_recency == 1.0.

    Observed indirectly: a never-shown message beats a recently-shown
    message even when its send_recency is identical.
    """
    event_log = _FakeEventLog()  # empty
    now = 1_000_000.0
    shown = _msg("shown", "2026-07-05T10:00:00Z")
    fresh = _msg("fresh", "2026-07-05T11:00:00Z")
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "shown",
            "timestamp": now - 60.0,  # shown 60s ago
            "sent_at": shown.sent_at_epoch(),
        }
    )
    picked = MessageSelector().pick([shown, fresh], now=now, event_log=event_log)
    assert picked is not None
    # `fresh` has display_recency=1.0, `shown` has display_recency < 1.0;
    # even with `shown` slightly newer on send_recency, `fresh` wins
    # on display_recency alone (W_DISPLAY=0.6 dominates W_SEND=0.3 at
    # this recency gap).
    assert picked.id == "fresh"


def test_two_never_shown_messages_have_identical_display_recency():
    """Two never-shown messages with different sent_at: send_recency decides."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    older = _msg("older", "2026-07-04T10:00:00Z")
    newer = _msg("newer", "2026-07-05T10:00:00Z")
    picked = MessageSelector().pick([older, newer], now=now, event_log=event_log)
    assert picked is not None
    # Both have display_recency = 1.0; newer wins on send_recency.
    assert picked.id == "newer"


# --- 6.7 Unit test: display_recency for a message with a recent event returns a value < 1.0 ---


def test_display_recency_reduces_for_recently_shown():
    """6.7: a recently-shown message has display_recency < 1.0.

    Same sent_at on both messages ties send_recency; the never-shown
    message wins on display_recency.
    """
    now = 1_000_000.0
    a = _msg("a", "2026-07-05T10:00:00Z")
    b = _msg("b", "2026-07-05T10:00:00Z")  # same sent_at
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "a",
                "timestamp": now - 60.0,
                "sent_at": a.sent_at_epoch(),
            }
        ]
    )
    picked = MessageSelector().pick([a, b], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "b"


def test_display_recency_value_at_known_age():
    """Spec scenario: 1h-ago show + 24h saturation → ~0.958.

    Construct a scenario where the gap matters: the shown message
    has been shown 1 hour ago, the fresh message was sent very
    recently. With W_DISPLAY=0.6 and W_SEND=0.3, the fresh
    message's display_recency of 1.0 combined with its high
    send_recency beats the shown message's display_recency of
    ~0.958 even though the shown message is also recent.
    """
    now = 1_000_000.0
    shown = _msg("shown", "2026-07-05T09:00:00Z")  # older, but shown
    fresh2 = _msg("fresh2", "2026-07-05T10:30:00Z")  # newer, never shown
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "shown",
                "timestamp": now - 3600.0,  # shown 1 hour ago
                "sent_at": shown.sent_at_epoch(),
            }
        ]
    )
    picked = MessageSelector().pick([shown, fresh2], now=now, event_log=event_log)
    assert picked is not None
    # fresh2: display_recency=1.0, send_recency=1.0
    # shown: display_recency=1-3600/86400≈0.958, send_recency=0.0
    # fresh2 wins clearly: 0.6*1.0 + 0.3*1.0 = 0.9 vs 0.6*0.958 + 0.3*0.0 = 0.575
    assert picked.id == "fresh2"


# --- 6.8 Unit test: display_recency is per-event-type ---


def test_display_recency_is_per_event_type():
    """6.8: a text_display event does not reduce image_display's display_recency."""
    now = 1_000_000.0
    a = _msg("a", "2026-07-05T10:00:00Z")
    b = _msg("b", "2026-07-05T10:00:00Z")
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "a",
                "timestamp": now - 60.0,
                "sent_at": a.sent_at_epoch(),
            }
        ]
    )

    selector = MessageSelector()

    # For `image_display`: message `a` has NO matching event →
    # display_recency = 1.0 → both tied on display_recency,
    # tied on sent_at → tie-breaker by id (lower first).
    image_pick = selector.pick([a, b], now=now, event_log=event_log, current_event_type="image_display")
    assert image_pick is not None
    assert image_pick.id == "a"

    # For `text_display`: message `a` was shown 60s ago →
    # display_recency < 1.0; `b` is never-shown → display_recency = 1.0.
    # `b` wins on display_recency alone.
    text_pick = selector.pick([a, b], now=now, event_log=event_log, current_event_type="text_display")
    assert text_pick is not None
    assert text_pick.id == "b"


def test_image_display_isolated_from_text_display_log():
    """An image_display selector reading a log with only text_display
    events for a message treats that message as never-shown for image
    purposes (so it ties with a never-shown message on display_recency).
    """
    now = 1_000_000.0
    a = _msg("a", "2026-07-05T10:00:00Z")
    b = _msg("b", "2026-07-05T10:00:00Z")
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "a",
                "timestamp": now - 60.0,
                "sent_at": a.sent_at_epoch(),
            }
        ]
    )
    picked = MessageSelector().pick([a, b], now=now, event_log=event_log, current_event_type="image_display")
    assert picked is not None
    # Tied on display_recency and sent_at → tie-breaker by id.
    assert picked.id == "a"


# --- 6.9 Unit test: send_recency returns 1.0 for the newest eligible and 0.0 for the oldest ---


def test_send_recency_endpoints():
    """6.9: newest eligible gets 1.0, oldest eligible gets 0.0.

    Indirectly observable: never-shown messages, the newest wins
    on send_recency alone.
    """
    event_log = _FakeEventLog()
    now = 1_000_000.0
    older = _msg("older", "2026-07-04T10:00:00Z")
    newer = _msg("newer", "2026-07-05T10:00:00Z")
    picked = MessageSelector().pick([older, newer], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "newer"


def test_send_recency_normalizes_over_eligible_set():
    """The newest eligible in a 5-message set wins on send_recency alone."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    msgs = [_msg(f"m{i}", f"2026-07-0{i + 1}T10:00:00Z") for i in range(5)]
    picked = MessageSelector().pick(msgs, now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m4"  # newest


# --- 6.10 Unit test: messages older than OFFSET_SECONDS are excluded from the eligible set ---


def test_eligibility_excludes_old_messages(monkeypatch):
    """6.10: messages older than OFFSET_SECONDS are not in the eligible set.

    Patch OFFSET_SECONDS to 60s for the test so we don't need real-world durations.

    Reimports `lib_shared.selector` and rebinds `MessageSelector` to the
    current module instance before monkeypatching, so the patch hits the
    same module dict the function reads from. (Earlier tests in this
    suite, including `test_auth.py`'s `app` fixture, swap `lib_shared.*`
    in `sys.modules` for mocks; conftest's `_restore_lib_shared` autouse
    fixture reimports them when they go Mock, leaving the `MessageSelector`
    captured at this test module's import time bound to a stale module
    dict. Re-resolving here makes the test robust against that cycle.)
    """
    import importlib

    import lib_shared.selector as selector_mod  # noqa: F401  (resolves sys.modules)

    selector_mod = importlib.import_module("lib_shared.selector")
    monkeypatch.setattr(selector_mod, "OFFSET_SECONDS", 60.0)
    event_log = _FakeEventLog()
    now = 1_000_000.0
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    eligible_dt = now_dt - timedelta(seconds=30)
    too_old_dt = now_dt - timedelta(seconds=90)
    eligible = _msg("m1", eligible_dt.isoformat().replace("+00:00", "Z"))
    too_old = _msg("m2", too_old_dt.isoformat().replace("+00:00", "Z"))

    picked = selector_mod.MessageSelector().pick([eligible, too_old], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m1"


def test_empty_eligible_set_returns_none(monkeypatch):
    """When no message is eligible, pick() returns None.

    See `test_eligibility_excludes_old_messages` for why we reimport the
    module here — sibling tests can leave the captured `MessageSelector`
    bound to a stale module dict.
    """
    import importlib

    import lib_shared.selector as selector_mod  # noqa: F401  (resolves sys.modules)

    selector_mod = importlib.import_module("lib_shared.selector")
    monkeypatch.setattr(selector_mod, "OFFSET_SECONDS", 60.0)
    event_log = _FakeEventLog()
    now = 1_000_000.0
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    too_old_dt = now_dt - timedelta(seconds=120)
    too_old = _msg("m1", too_old_dt.isoformat().replace("+00:00", "Z"))

    picked = selector_mod.MessageSelector().pick([too_old], now=now, event_log=event_log)
    assert picked is None


def test_messages_within_offset_are_eligible():
    """A message sent 7 days ago (within 14-day default) is eligible."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    seven_days_ago = (now_dt - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    m = _msg("m1", seven_days_ago)
    picked = MessageSelector().pick([m], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m1"


# --- 6.11 Unit test: a favorite with the same recency as a non-favorite beats the non-favorite ---


def test_favorite_with_same_recency_beats_non_favorite():
    """6.11: a favorite beats a non-favorite at identical recency."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    # Same sent_at → tied on send_recency.
    # Empty log → both have display_recency = 1.0.
    fav = _msg("fav", "2026-07-05T10:00:00Z")
    non_fav = _msg("non_fav", "2026-07-05T10:00:00Z")
    selector = MessageSelector(favorites=["fav"])
    picked = selector.pick([non_fav, fav], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "fav"


def test_per_call_favorites_override_constructor():
    """`pick(favorites=...)` overrides the constructor's favorites for that call."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    fav = _msg("fav", "2026-07-05T10:00:00Z")
    other = _msg("other", "2026-07-05T10:00:00Z")
    selector = MessageSelector(favorites=["fav"])
    # Override: call with `favorites=["other"]` → other wins.
    picked = selector.pick([fav, other], now=now, event_log=event_log, favorites=["other"])
    assert picked is not None
    assert picked.id == "other"


def test_favorite_boost_dominates_recency_gap_at_edge():
    """At the recency endpoint (oldest=0.0, never shown=1.0), the favorite
    boost alone (W_FAVORITE=0.4) is enough to overcome a non-favorite's
    W_DISPLAY+W_SEND advantage (0.6 + 0.3 = 0.9).
    But at a close recency gap, favorite tilt can flip the outcome.
    """
    event_log = _FakeEventLog()
    now = 1_000_000.0
    # favorite is older than non_favorite — favorite boost dominates.
    fav = _msg("fav", "2026-07-04T10:00:00Z")  # older, send_recency = 0.0
    non_fav = _msg("non_fav", "2026-07-05T10:00:00Z")  # newer, send_recency = 1.0
    selector = MessageSelector(favorites=["fav"])
    picked = selector.pick([fav, non_fav], now=now, event_log=event_log)
    assert picked is not None
    # fav: 0.6 * 1.0 + 0.3 * 0.0 + 0.4 * 1.0 = 1.0
    # non_fav: 0.6 * 1.0 + 0.3 * 1.0 + 0.4 * 0.0 = 0.9
    assert picked.id == "fav"


# --- 6.12 Unit test: deterministic pick — same inputs always produce the same output ---


def test_deterministic_pick_with_fixed_inputs():
    """6.12: same inputs always produce the same output."""
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "m1",
                "timestamp": 999_900.0,
                "sent_at": 999_000.0,
            }
        ]
    )
    messages = [_msg(f"m{i}", f"2026-07-0{i + 1}T10:00:00Z") for i in range(5)]
    now = 1_000_000.0

    selector = MessageSelector()
    first = selector.pick(messages, now=now, event_log=event_log)
    second = selector.pick(messages, now=now, event_log=event_log)
    third = selector.pick(messages, now=now, event_log=event_log)
    assert first is not None
    assert second is not None
    assert third is not None
    assert first.id == second.id == third.id


def test_pick_returns_none_for_empty_input():
    """Empty message list returns None."""
    picked = MessageSelector().pick([], now=1_000_000.0, event_log=_FakeEventLog())
    assert picked is None


# --- 6.13 Unit test: stable tie-breaker — identical scores resolve by (sent_at, id) ---


def test_tie_breaker_uses_sent_at_then_id():
    """6.13: identical scores → tie-breaker by (-score, sent_at, id).

    Two messages with identical sent_at + display_recency → tied on
    score → tie-breaker on id (lower first).
    """
    event_log = _FakeEventLog()
    now = 1_000_000.0
    a = _msg("aaa", "2026-07-05T10:00:00Z")
    z = _msg("zzz", "2026-07-05T10:00:00Z")
    picked = MessageSelector().pick([z, a], now=now, event_log=event_log)
    assert picked is not None
    # Tied on score, tied on sent_at → tie-breaker on id → "aaa" wins.
    assert picked.id == "aaa"


def test_tie_breaker_only_kicks_in_on_identical_score():
    """When display_recency differs, the higher display_recency wins
    regardless of sent_at. The tie-breaker only kicks in on identical scores.
    """
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "old",
                "timestamp": 999_900.0,
                "sent_at": 999_000.0,
            }
        ]
    )
    now = 1_000_000.0
    old = _msg("old", "2026-07-05T10:00:00Z")  # shown recently → low display_recency
    fresh = _msg("fresh", "2026-07-05T11:00:00Z")  # never shown → display_recency = 1.0
    picked = MessageSelector().pick([old, fresh], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "fresh"


# --- 6.15 Integration test: renderer writes an event after advancing; subsequent selector sees updated display-recency ---


def test_integration_renderer_writes_event_then_selector_observes_it(tmp_path):
    """6.15: integration of selector + EventLog.

    An appended event changes the next pick: the just-shown message
    loses display_recency; the never-shown message wins.

    Construct a scenario where the display_recency decay is large
    enough to flip the pick: the first message is shown 12 hours
    ago (display_recency ≈ 0.5); the second message is never
    shown (display_recency = 1.0). With W_DISPLAY=0.6 and
    W_SEND=0.3 and identical sent_at, the never-shown message's
    score advantage (0.6 * (1.0 - 0.5) = 0.3) dominates the
    send_recency tie.
    """
    # Use the real EventLog class.
    from event_log import EventLog

    log_path = tmp_path / "events.jsonl"
    event_log = EventLog(path=str(log_path), max_entries=10)

    now = 1_000_000.0
    a = _msg("a", "2026-07-05T10:00:00Z")
    b = _msg("b", "2026-07-05T10:00:00Z")

    # Pre-populate the log: a was shown 12 hours ago. Without this,
    # both messages have display_recency = 1.0 and the test would
    # be a tie-breaker exercise, not a display_recency one.
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "a",
            "timestamp": now - 43_200.0,  # 12 hours ago → display_recency ≈ 0.5
            "sent_at": a.sent_at_epoch(),
        }
    )

    # Pick — `b` is never-shown (display_recency = 1.0); `a` has
    # display_recency ≈ 0.5. With identical sent_at, send_recency
    # is tied. b wins on display_recency alone.
    picked = MessageSelector().pick([a, b], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "b"

    # Now the renderer "writes" an event for `b` 12 hours ago — the
    # next pick should fall back to `a` (now the never-shown one).
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "b",
            "timestamp": now - 43_200.0,  # 12 hours ago
            "sent_at": b.sent_at_epoch(),
        }
    )
    second = MessageSelector().pick([a, b], now=now, event_log=event_log)
    assert second is not None
    assert second.id == "a"


# --- 6.18 Unit test: constants are importable from lib_shared.selector with documented defaults ---


def test_constants_have_documented_defaults():
    """6.18: the selector's behavioral knobs are importable with documented defaults."""
    # Re-import to assert that the module exports them and they
    # match the design values. We assert specific numeric values
    # so any accidental tuning change shows up in CI.
    assert W_DISPLAY == 0.6
    assert W_SEND == 0.3
    assert W_FAVORITE == 0.4
    assert SATURATION_SECONDS == 86_400.0
    assert OFFSET_SECONDS == 1_209_600.0
    assert USE_WEIGHTED_SELECTOR is False


def test_use_weighted_selector_flag_defaults_false():
    """Documented rollout flag default — ships dark."""
    assert USE_WEIGHTED_SELECTOR is False


# --- 6.19: browser preview uses the same MessageSelector class ---


def test_browser_preview_uses_same_selector_class():
    """6.19 (smoke): the selector lives in lib_shared and is importable
    from both runtimes — the same class runs in CPython (Pi) and
    PyScript (browser). This test verifies the class is the one
    named in CLAUDE.md and the spec, and that the constants are
    accessible at import time (PyScript requires module-level
    constants to be importable without side effects)."""
    import lib_shared.selector as _selector_mod

    # The class is named MessageSelector — that's the contract.
    assert hasattr(_selector_mod, "MessageSelector")
    assert _selector_mod.MessageSelector.__module__ == "lib_shared.selector"

    # Both runtimes see the same constants at module load time.
    assert _selector_mod.W_DISPLAY == 0.6
    assert _selector_mod.W_SEND == 0.3
    assert _selector_mod.W_FAVORITE == 0.4
    assert _selector_mod.USE_WEIGHTED_SELECTOR is False
