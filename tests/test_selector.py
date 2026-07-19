"""Tests for `lib_shared.selector` (issue #26).

Covers the `WeightedSelector` (tasks 6.6-6.13, 6.18, 6.15), the
`RandomSelector` (regression coverage that the historical behavior still
works), and 6.19 (browser preview shares the same selector class).
Event-log unit tests live in `test_event_log.py`; the coordinator-level
pre-emption test lives in `test_event_log_integration.py`.

The previous `MessageSelector()` direct instantiation is gone — the base
class is abstract. Tests now exercise `WeightedSelector()` (the
production algorithm) and `RandomSelector()` (the historical rotation)
explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add the repo root + heart-matrix-controller to sys.path so the
# `event_log` module is importable from this test.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "heart-matrix-controller"))

import pytest  # noqa: E402

from lib_shared.models import Message  # noqa: E402
from lib_shared.selector import (  # noqa: E402
    OFFSET_SECONDS,
    SATURATION_SECONDS,
    USE_WEIGHTED_SELECTOR,
    W_DISPLAY,
    W_FAVORITE,
    W_SEND,
    MessageSelector,
    RandomSelector,
    WeightedSelector,
)

# --- helpers ---


def _msg(message_id: str, received_at_iso: str, body: str = "hello") -> Message:
    """Build a Message with the given id and ISO 8601 received_at."""
    return Message(id=message_id, sender="+15551234567", body=body, received_at=received_at_iso)


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
            "received_at": shown.received_at_epoch(),
        }
    )
    picked = WeightedSelector().pick([shown, fresh], now=now, event_log=event_log)
    assert picked is not None
    # `fresh` has display_recency=1.0, `shown` has display_recency < 1.0;
    # even with `shown` slightly newer on send_recency, `fresh` wins
    # on display_recency alone (W_DISPLAY=0.6 dominates W_SEND=0.3 at
    # this recency gap).
    assert picked.id == "fresh"


def test_two_never_shown_messages_have_identical_display_recency():
    """Two never-shown messages with different received_at: send_recency decides."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    older = _msg("older", "2026-07-04T10:00:00Z")
    newer = _msg("newer", "2026-07-05T10:00:00Z")
    picked = WeightedSelector().pick([older, newer], now=now, event_log=event_log)
    assert picked is not None
    # Both have display_recency = 1.0; newer wins on send_recency.
    assert picked.id == "newer"


# --- 6.7 Unit test: display_recency for a message with a recent event returns a value < 1.0 ---


def test_display_recency_reduces_for_recently_shown():
    """6.7: a recently-shown message has display_recency < 1.0.

    Same received_at on both messages ties send_recency; the never-shown
    message wins on display_recency. (display_recency for a just-shown
    message is now ~0.0; never-shown is 1.0 — the inversion means the
    freshness arm favors long-ago or never-shown.)
    """
    now = 1_000_000.0
    a = _msg("a", "2026-07-05T10:00:00Z")
    b = _msg("b", "2026-07-05T10:00:00Z")  # same received_at
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "a",
                "timestamp": now - 60.0,
                "received_at": a.received_at_epoch(),
            }
        ]
    )
    picked = WeightedSelector().pick([a, b], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "b"


def test_display_recency_value_at_known_age():
    """Spec scenario: 1h-ago show + 24h saturation → ~0.042.

    Construct a scenario where the gap matters: the shown message
    has been shown 1 hour ago, the fresh message was sent very
    recently. With W_DISPLAY=0.6 and W_SEND=0.3, the fresh
    message's display_recency of 1.0 combined with its high
    send_recency beats the shown message's low display_recency
    even though the shown message is also recent.
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
                "received_at": shown.received_at_epoch(),
            }
        ]
    )
    picked = WeightedSelector().pick([shown, fresh2], now=now, event_log=event_log)
    assert picked is not None
    # fresh2: display_recency=1.0, send_recency=1.0
    # shown: display_recency=3600/86400≈0.042, send_recency=0.0
    # fresh2 wins clearly: 0.6*1.0 + 0.3*1.0 = 0.9 vs 0.6*0.042 + 0.3*0.0 = 0.025
    assert picked.id == "fresh2"


def test_just_shown_message_sits_out():
    """A message shown at `now` (age ≈ 0) has display_recency ≈ 0.0 —
    it sits out, and the never-shown message wins on the freshness arm.

    This pins the inversion: a just-shown message must NOT tie with a
    never-shown message on display_recency (the old formula returned
    1.0 for both, which let the just-shown message win on send_recency
    and re-pick itself).
    """
    now = 1_000_000.0
    just_shown = _msg("just_shown", "2026-07-05T10:00:00Z")
    never_shown = _msg("never_shown", "2026-07-05T10:00:01Z")  # same received_at, 1s later
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "just_shown",
                "timestamp": now,  # shown EXACTLY now
                "received_at": just_shown.received_at_epoch(),
            }
        ]
    )
    picked = WeightedSelector().pick([just_shown, never_shown], now=now, event_log=event_log)
    assert picked is not None
    # just_shown: display_recency=0.0, send_recency=0.0
    # never_shown: display_recency=1.0, send_recency=1.0
    # never_shown wins: 0.6*1.0 + 0.3*1.0 = 0.9 vs 0.6*0.0 + 0.3*0.0 = 0.0
    assert picked.id == "never_shown"


def test_display_recency_is_zero_for_age_zero():
    """Pin the edge case: a message with age=0 has display_recency=0.0.

    Direct unit probe (not via the full pick) — exercises the formula
    boundary explicitly so future regressions trip loudly.
    """
    now = 1_000_000.0
    msg = _msg("msg", "2026-07-05T10:00:00Z")
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "msg",
                "timestamp": now,
                "received_at": msg.received_at_epoch(),
            }
        ]
    )
    recency = WeightedSelector._display_recency(msg, now, event_log, "text_display")
    assert recency == 0.0


def test_display_recency_is_one_at_saturation():
    """Pin the other edge: a message at SATURATION has display_recency=1.0.

    Once the gap hits the saturation window, the message is "as
    pickable" as a never-shown one — ready to surface.
    """
    from lib_shared.selector import SATURATION_SECONDS

    now = 1_000_000.0
    msg = _msg("msg", "2026-07-05T10:00:00Z")
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "msg",
                "timestamp": now - SATURATION_SECONDS,
                "received_at": msg.received_at_epoch(),
            }
        ]
    )
    recency = WeightedSelector._display_recency(msg, now, event_log, "text_display")
    assert recency == 1.0


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
                "received_at": a.received_at_epoch(),
            }
        ]
    )

    selector = WeightedSelector(event_type="text_display")

    # For `image_display`: message `a` has NO matching event →
    # display_recency = 1.0 → both tied on display_recency,
    # tied on received_at → tie-breaker by id (lower first).
    image_pick = selector.pick([a, b], now=now, event_log=event_log, event_type="image_display")
    assert image_pick is not None
    assert image_pick.id == "a"

    # For `text_display`: message `a` was shown 60s ago →
    # display_recency < 1.0; `b` is never-shown → display_recency = 1.0.
    # `b` wins on display_recency alone.
    text_pick = selector.pick([a, b], now=now, event_log=event_log, event_type="text_display")
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
                "received_at": a.received_at_epoch(),
            }
        ]
    )
    selector = WeightedSelector(event_type="image_display")
    picked = selector.pick([a, b], now=now, event_log=event_log)
    assert picked is not None
    # Tied on display_recency and received_at → tie-breaker by id.
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
    picked = WeightedSelector().pick([older, newer], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "newer"


def test_send_recency_normalizes_over_eligible_set():
    """The newest eligible in a 5-message set wins on send_recency alone."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    msgs = [_msg(f"m{i}", f"2026-07-0{i + 1}T10:00:00Z") for i in range(5)]
    picked = WeightedSelector().pick(msgs, now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m4"  # newest


# --- 6.10 Unit test: messages older than OFFSET_SECONDS are excluded from the eligible set ---


def test_eligibility_excludes_old_messages(monkeypatch):
    """6.10: messages older than OFFSET_SECONDS are not in the eligible set.

    Patch OFFSET_SECONDS to 60s for the test so we don't need real-world durations.

    Reimports `lib_shared.selector` and rebinds `WeightedSelector` to the
    current module instance before monkeypatching, so the patch hits the
    same module dict the function reads from.
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

    picked = selector_mod.WeightedSelector().pick([eligible, too_old], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m1"


def test_empty_eligible_set_returns_none(monkeypatch):
    """When no message is eligible, pick() returns None.

    Reimports `lib_shared.selector` so monkeypatch hits the live module.
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

    picked = selector_mod.WeightedSelector().pick([too_old], now=now, event_log=event_log)
    assert picked is None


def test_messages_within_offset_are_eligible():
    """A message sent 7 days ago (within 14-day default) is eligible."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    from datetime import datetime, timedelta, timezone

    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    seven_days_ago = (now_dt - timedelta(days=7)).isoformat().replace("+00:00", "Z")
    m = _msg("m1", seven_days_ago)
    picked = WeightedSelector().pick([m], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "m1"


# --- 6.11 Unit test: a favorite with the same recency as a non-favorite beats the non-favorite ---


def test_favorite_with_same_recency_beats_non_favorite():
    """6.11: a favorite beats a non-favorite at identical recency."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    # Same received_at → tied on send_recency.
    # Empty log → both have display_recency = 1.0.
    fav = _msg("fav", "2026-07-05T10:00:00Z")
    non_fav = _msg("non_fav", "2026-07-05T10:00:00Z")
    selector = WeightedSelector(favorites=["fav"])
    picked = selector.pick([non_fav, fav], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "fav"


def test_per_call_favorites_override_constructor():
    """`pick(favorites=...)` overrides the constructor's favorites for that call."""
    event_log = _FakeEventLog()
    now = 1_000_000.0
    fav = _msg("fav", "2026-07-05T10:00:00Z")
    other = _msg("other", "2026-07-05T10:00:00Z")
    selector = WeightedSelector(favorites=["fav"])
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
    selector = WeightedSelector(favorites=["fav"])
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
                "received_at": 999_000.0,
            }
        ]
    )
    messages = [_msg(f"m{i}", f"2026-07-0{i + 1}T10:00:00Z") for i in range(5)]
    now = 1_000_000.0

    selector = WeightedSelector()
    first = selector.pick(messages, now=now, event_log=event_log)
    second = selector.pick(messages, now=now, event_log=event_log)
    third = selector.pick(messages, now=now, event_log=event_log)
    assert first is not None
    assert second is not None
    assert third is not None
    assert first.id == second.id == third.id


def test_pick_returns_none_for_empty_input():
    """Empty message list returns None."""
    picked = WeightedSelector().pick([], now=1_000_000.0, event_log=_FakeEventLog())
    assert picked is None


# --- 6.13 Unit test: stable tie-breaker — identical scores resolve by (received_at, id) ---


def test_tie_breaker_uses_received_at_then_id():
    """6.13: identical scores → tie-breaker by (-score, received_at, id).

    Two messages with identical received_at + display_recency → tied on
    score → tie-breaker on id (lower first).
    """
    event_log = _FakeEventLog()
    now = 1_000_000.0
    a = _msg("aaa", "2026-07-05T10:00:00Z")
    z = _msg("zzz", "2026-07-05T10:00:00Z")
    picked = WeightedSelector().pick([z, a], now=now, event_log=event_log)
    assert picked is not None
    # Tied on score, tied on received_at → tie-breaker on id → "aaa" wins.
    assert picked.id == "aaa"


def test_tie_breaker_only_kicks_in_on_identical_score():
    """When display_recency differs, the higher display_recency wins
    regardless of received_at. The tie-breaker only kicks in on identical scores.
    """
    event_log = _FakeEventLog(
        [
            {
                "event_type": "text_display",
                "message_id": "old",
                "timestamp": 999_900.0,
                "received_at": 999_000.0,
            }
        ]
    )
    now = 1_000_000.0
    old = _msg("old", "2026-07-05T10:00:00Z")  # shown recently → low display_recency
    fresh = _msg("fresh", "2026-07-05T11:00:00Z")  # never shown → display_recency = 1.0
    picked = WeightedSelector().pick([old, fresh], now=now, event_log=event_log)
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
    W_SEND=0.3 and identical received_at, the never-shown message's
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
            "received_at": a.received_at_epoch(),
        }
    )

    # Pick — `b` is never-shown (display_recency = 1.0); `a` has
    # display_recency ≈ 0.5. With identical received_at, send_recency
    # is tied. b wins on display_recency alone.
    picked = WeightedSelector().pick([a, b], now=now, event_log=event_log)
    assert picked is not None
    assert picked.id == "b"

    # Now the renderer "writes" an event for `b` 12 hours ago — the
    # next pick should fall back to `a` (now the never-shown one).
    event_log.append(
        {
            "event_type": "text_display",
            "message_id": "b",
            "timestamp": now - 43_200.0,  # 12 hours ago
            "received_at": b.received_at_epoch(),
        }
    )
    second = WeightedSelector().pick([a, b], now=now, event_log=event_log)
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
    assert USE_WEIGHTED_SELECTOR is True


def test_use_weighted_selector_flag_defaults_true():
    """Default-selector rollout flag — flipped to True 2026-07-18
    after the "same message picked back-to-back" symptom observed
    in the browser preview. `WeightedSelector.display_recency`
    penalizes just-shown messages; `RandomSelector` had no such
    mechanism. `RandomSelector` stays in the file as the operator
    opt-out (Default-to-X-keeps-Y rule)."""
    assert USE_WEIGHTED_SELECTOR is True


# --- 6.19: browser preview uses the same MessageSelector class ---


def test_browser_preview_uses_same_selector_class():
    """6.19 (smoke): the selector lives in lib_shared and is importable
    from both runtimes — the same class runs in CPython (Pi) and
    PyScript (browser). This test verifies the class is the one
    named in CLAUDE.md and the spec, and that the constants are
    accessible at import time (PyScript requires module-level
    constants to be importable without side effects)."""
    import lib_shared.selector as _selector_mod

    # The selector module exports the abstract base + both concrete
    # implementations. The base class is named MessageSelector — that's
    # the contract.
    assert hasattr(_selector_mod, "MessageSelector")
    assert _selector_mod.MessageSelector.__module__ == "lib_shared.selector"

    # Both runtimes see the same constants at module load time.
    assert _selector_mod.W_DISPLAY == 0.6
    assert _selector_mod.W_SEND == 0.3
    assert _selector_mod.W_FAVORITE == 0.4
    assert _selector_mod.USE_WEIGHTED_SELECTOR is True


# --- MessageSelector ABC is abstract ---


def test_message_selector_base_cannot_be_instantiated_directly():
    """The ABC raises on direct construction — callers must pick a
    concrete subclass."""
    with pytest.raises(TypeError):
        MessageSelector()  # type: ignore[abstract]


def test_random_selector_is_a_message_selector_subclass():
    """Both subclasses register under the ABC's isinstance contract."""
    assert isinstance(RandomSelector(), MessageSelector)
    assert isinstance(WeightedSelector(), MessageSelector)


# --- RandomSelector: historical rotation behavior preserved ---


def test_random_selector_returns_none_for_empty_pool():
    """Empty input → None, matches the historical rotation contract."""
    assert RandomSelector().pick([], now=1_000_000.0) is None


def test_random_selector_returns_a_member_of_the_pool():
    """`random.choice` semantics — return only what's in the pool."""
    import random as _random

    _random.seed(42)
    msgs = [_msg(f"m{i}", "2026-07-05T10:00:00Z") for i in range(3)]
    for _ in range(20):
        picked = RandomSelector().pick(msgs, now=1_000_000.0)
        assert picked is not None
        assert picked in msgs


def test_random_selector_ignores_event_log_and_favorites():
    """`RandomSelector` advertises non-determinism; it does NOT honor
    display_recency or favorite boost. Verify it ignores them by
    pinning `random.seed` and asserting the pick is purely random.
    """
    import random as _random

    _random.seed(0)
    # Non-empty log + favorites should NOT influence the pick — both
    # are documented as ignored by `RandomSelector`.
    msgs = [_msg("a", "2026-07-05T10:00:00Z"), _msg("b", "2026-07-05T10:00:00Z")]
    selector = RandomSelector()
    picked_with_events = selector.pick(
        msgs,
        now=1_000_000.0,
        event_log=_FakeEventLog(
            [
                {
                    "event_type": "text_display",
                    "message_id": "a",
                    "timestamp": 999_999.0,
                    "received_at": 999_000.0,
                }
            ]
        ),
        favorites=["a"],
    )
    _random.seed(0)
    picked_without = selector.pick(msgs, now=1_000_000.0)
    # Same seed → same `random.choice([a, b])` → same pick.
    assert picked_with_events is not None
    assert picked_without is not None
    assert picked_with_events.id == picked_without.id


# --- Co-existence: EffectsCoordinator is selector-agnostic ---


def test_effects_coordinator_accepts_weighted_and_random_via_kwarg():
    """The coordinator's `selector` kwarg accepts any `MessageSelector`
    subclass. Both concrete classes round-trip.

    Mirrors the `_StubManager` pattern used by
    `test_event_log_integration.py`. We only verify that the constructor
    accepts both selector types — coordinator behavior is covered by
    the integration tests, not by selector tests.
    """
    from types import SimpleNamespace

    from lib_shared.effects_coordinator import EffectsCoordinator
    from lib_shared.models import EffectsSettings, TextSettings

    class _StubManager:
        """Minimal MessageManager-shaped stub for selector wiring tests."""

        config = SimpleNamespace(
            effects_settings=EffectsSettings(),
            text_settings=TextSettings(),
        )

        def get_messages(self, limit: int = 10, suppress: bool = True) -> list:
            del limit, suppress
            return []

        def get_effects_settings(self) -> EffectsSettings:
            return self.config.effects_settings

        def get_text_settings(self) -> TextSettings:
            return self.config.text_settings

    manager = _StubManager()
    EffectsCoordinator(
        message_manager=manager,  # type: ignore[reportArgumentType]  # _StubManager is duck-typed MessageManager
        selector=WeightedSelector(),
        event_log=_FakeEventLog(),
    )
    EffectsCoordinator(
        message_manager=manager,  # type: ignore[reportArgumentType]  # _StubManager is duck-typed MessageManager
        selector=RandomSelector(),
        event_log=_FakeEventLog(),
    )


# --- exclude_id: anti-repeat hint at the coordinator call site ---


def test_random_selector_excludes_id_from_candidate_pool():
    """`exclude_id` drops the matching message from the candidate pool
    before `random.choice`. Pins the anti-repeat contract: the
    coordinator passes the just-consumed message's id so the next
    pick doesn't re-pick the same message back-to-back.

    Regression for the "image isn't always rendering reliably" +
    "same message gets selected back-to-back" symptoms observed in
    the browser preview 2026-07-18: with `RandomSelector` (the
    default `USE_WEIGHTED_SELECTOR=False`) and a 2-message buffer,
    uniform random over `[m1, m2]` produces `m1` ~50% of the time,
    including right after `m1` was just shown. The downstream
    cycler-suppress guard (same-id discriminator) then intentionally
    skips the cycler rebuild and the image fails to render.
    Filtering `exclude_id` out of the candidate pool breaks the
    cycle at the source.
    """
    import random as _random

    _random.seed(0)
    msgs = [
        _msg("m1", "2026-07-05T10:00:00Z"),
        _msg("m2", "2026-07-05T10:01:00Z"),
    ]
    selector = RandomSelector()
    # Pin the seed so the random.choice is deterministic across runs;
    # without exclude_id, the pick may return m1; with exclude_id="m1",
    # the only remaining candidate is m2.
    for _ in range(20):
        picked = selector.pick(msgs, now=1_000_000.0, exclude_id="m1")
        assert picked is not None
        assert picked.id == "m2", f"exclude_id='m1' should drop m1 from the candidate pool; got {picked.id!r}"


def test_random_selector_falls_back_when_exclusion_empties_pool():
    """When `exclude_id` is the only candidate's id, the unfiltered
    pool is used so the sign keeps rotating. The sign must not go
    dark just because the currently-rendered message happens to be
    the only one available — the same behavior the historical
    rotation had when the buffer held a single message.
    """
    import random as _random

    _random.seed(0)
    msgs = [_msg("only", "2026-07-05T10:00:00Z")]
    picked = RandomSelector().pick(msgs, now=1_000_000.0, exclude_id="only")
    assert picked is not None
    assert picked.id == "only", (
        f"when the only candidate matches exclude_id, the unfiltered pool must "
        f"be used so the sign doesn't go dark; got {picked.id!r}"
    )


def test_random_selector_exclude_id_unknown_does_not_drop_everything():
    """`exclude_id` that doesn't match any candidate is a no-op —
    the candidate pool is unchanged. Defensive against the
    coordinator passing a stale id that no longer matches the
    buffer (e.g. message evicted from the recent_count window).
    """
    import random as _random

    _random.seed(42)
    msgs = [_msg("a", "2026-07-05T10:00:00Z"), _msg("b", "2026-07-05T10:00:01Z")]
    picked = RandomSelector().pick(msgs, now=1_000_000.0, exclude_id="not-in-pool")
    assert picked is not None
    assert picked.id in {"a", "b"}


def test_weighted_selector_excludes_id_from_candidate_pool():
    """`WeightedSelector` honors `exclude_id` after the eligibility
    check, before scoring. The `display_recency` component already
    penalizes just-shown messages, but the explicit `exclude_id`
    filter is a defensive double-check that doesn't change the
    algorithm's other invariants — and keeps the API consistent
    with `RandomSelector`.
    """
    now = 1_000_000.0
    msgs = [
        _msg("m1", "2026-07-05T10:00:00Z"),
        _msg("m2", "2026-07-05T10:01:00Z"),
    ]
    picked = WeightedSelector().pick(msgs, now=now, exclude_id="m1")
    assert picked is not None
    assert picked.id == "m2", f"exclude_id='m1' should drop m1 from the candidate pool; got {picked.id!r}"


def test_weighted_selector_falls_back_when_exclusion_empties_pool():
    """Same as `RandomSelector` — when the only eligible candidate
    matches `exclude_id`, the unfiltered eligible set is used so
    the sign keeps rotating.
    """
    now = 1_000_000.0
    msgs = [_msg("only", "2026-07-05T10:00:00Z")]
    picked = WeightedSelector().pick(msgs, now=now, exclude_id="only")
    assert picked is not None
    assert picked.id == "only"
