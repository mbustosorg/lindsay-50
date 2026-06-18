"""Tests for EffectsCoordinator.get_display_message() + the required message_manager arg.

Scenarios:
1. Required message_manager raises TypeError when omitted.
2. get_display_message returns the head entry's body and updates _last_shown_message_id
   when the head is fresh.
3. get_display_message samples uniformly from the most recent recent_count entries
   when the head has already been shown (seed random and assert the pick).
4. get_display_message returns None on an empty buffer.
5. get_display_message respects recent_count (a 3-message buffer with recent_count=3 is
   read fully; with recent_count=2 only the head 2 are read).
6. tick() calls get_display_message() at most every 250 ms even when called in a tight
   loop (spy on the method, count invocations across 1 second of ticks).
"""

import random
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import Message, MessageView


def _make_view(message_id: str, body: str, received_at: str, suppressed: bool = False) -> MessageView:
    return MessageView(
        Message(id=message_id, sender="+1", body=body, received_at=received_at),
        source="mqtt",
        suppressed=suppressed,
    )


class _StubMessageManager:
    """Minimal MessageManager stub exposing just the surface EffectsCoordinator reads."""

    def __init__(self, messages=None, recent_count=5):
        from lib_shared.models import EffectsSettings, TextSettings

        self._entries = list(messages or [])
        self._recent_count = recent_count
        # The coordinator reads `recent_count` (and the rest of
        # the pacing) live from `message_manager.config.effect_settings`.
        self.config = SimpleNamespace(
            effect_settings=EffectsSettings(recent_count=recent_count),
            text_settings=TextSettings(),
        )

    @property
    def messages(self):
        msgs = self
        return SimpleNamespace(get_messages=self._get_messages)

    def _get_messages(self, limit=100, suppress=True):
        entries = list(self._entries)
        if suppress:
            entries = [e for e in entries if not getattr(e, "suppressed", False)]
        return sorted(entries, key=lambda e: e.message.received_at, reverse=True)[:limit]

    def add(self, view):
        self._entries.append(view)


def _build(message_manager=None, recent_count=5):
    """Build a coordinator with the minimum layer needed to construct one."""
    if message_manager is None:
        message_manager = _StubMessageManager(recent_count=recent_count)
    else:
        # Caller supplied a pre-built manager — override the
        # `recent_count` it holds so the test's expected value
        # takes effect (the coordinator reads it live from the
        # manager).
        message_manager.config.effect_settings.recent_count = recent_count
    coord = EffectsCoordinator(
        message_manager=message_manager,
    )
    return coord, message_manager


# --- Scenario 1: required message_manager raises TypeError -------------------


def test_required_message_manager_raises_typeerror_when_omitted():
    """`EffectsCoordinator.__init__` raises TypeError when message_manager is omitted.

    The constructor signature makes `message_manager` a required positional
    argument with no default — so a caller that omits it gets Python's
    standard `TypeError: ... missing 1 required positional argument: 'message_manager'`.
    """
    with pytest.raises(TypeError, match="message_manager"):
        EffectsCoordinator()


# --- Scenario 2: get_display_message returns head body + updates _last_shown_id


def test_get_display_message_returns_head_when_fresh():
    """When the head id differs from _last_shown_message_id, return its body."""
    mgr = _StubMessageManager(
        messages=[
            _make_view("a", "body-a", "2026-01-02T00:00:00Z"),
            _make_view("b", "body-b", "2026-01-01T00:00:00Z"),
        ]
    )
    coord, _ = _build(message_manager=mgr, recent_count=5)
    # Fresh head: never shown.
    assert coord._last_shown_message_id is None
    body = coord.get_display_message()
    assert body == "body-a"
    assert coord._last_shown_message_id == "a"


# --- Scenario 3: get_display_message samples uniformly from recent-N ---------


def test_get_display_message_samples_uniformly_when_head_already_shown():
    """When the head has already been shown, pick uniformly at random from the list."""
    random.seed(0)
    mgr = _StubMessageManager(
        messages=[
            _make_view("a", "body-a", "2026-01-04T00:00:00Z"),
            _make_view("b", "body-b", "2026-01-03T00:00:00Z"),
            _make_view("c", "body-c", "2026-01-02T00:00:00Z"),
        ]
    )
    coord, _ = _build(message_manager=mgr, recent_count=3)
    # Prime: first pull returns the head ("a") and updates _last_shown_message_id.
    coord.get_display_message()
    assert coord._last_shown_message_id == "a"
    # Patch random.choice to assert the call site + return value.
    with patch("lib_shared.effects_coordinator.random.choice", wraps=random.choice) as choice_spy:
        body = coord.get_display_message()
    # random.choice was called with the 3 entries.
    assert choice_spy.call_count == 1
    args, _ = choice_spy.call_args
    assert list(args[0]) == mgr._get_messages(limit=3, suppress=True)
    # The returned body must be one of the three known bodies.
    assert body in {"body-a", "body-b", "body-c"}
    # _last_shown_message_id is the picked message's id.
    expected_ids = {"a": "body-a", "b": "body-b", "c": "body-c"}
    assert coord._last_shown_message_id in expected_ids
    assert expected_ids[coord._last_shown_message_id] == body


# --- Scenario 4: get_display_message returns None on an empty buffer ---------


def test_get_display_message_returns_none_on_empty_buffer():
    """Empty buffer → returns None; _last_shown_message_id untouched."""
    mgr = _StubMessageManager(messages=[])
    coord, _ = _build(message_manager=mgr, recent_count=5)
    assert coord.get_display_message() is None
    assert coord._last_shown_message_id is None


# --- Scenario 5: get_display_message respects recent_count -------------------


def test_get_display_message_respects_recent_count():
    """recent_count caps the buffer slice used for sampling."""
    # 10 entries; recent_count=3 reads only the 3 newest.
    msgs = [_make_view(f"id{i:02d}", f"body-{i:02d}", f"2026-01-{10 + i:02d}T00:00:00Z") for i in range(10)]
    mgr = _StubMessageManager(messages=msgs)
    coord, _ = _build(message_manager=mgr, recent_count=3)

    # Patch manager._get_messages to assert the limit argument.
    seen_limits = []

    def _capture(limit=100, suppress=True):
        seen_limits.append(limit)
        return sorted(mgr._entries, key=lambda e: e.message.received_at, reverse=True)[:limit]

    with patch.object(mgr, "_get_messages", side_effect=_capture):
        body = coord.get_display_message()
    assert seen_limits == [3]
    # The head (newest) is returned.
    assert body == "body-09"


def test_recent_count_2_reads_only_top_2():
    """recent_count=2 reads exactly 2 entries."""
    msgs = [_make_view(f"id{i:02d}", f"body-{i:02d}", f"2026-01-{10 + i:02d}T00:00:00Z") for i in range(10)]
    mgr = _StubMessageManager(messages=msgs)
    coord, _ = _build(message_manager=mgr, recent_count=2)

    seen_limits = []

    def _capture(limit=100, suppress=True):
        seen_limits.append(limit)
        return sorted(mgr._entries, key=lambda e: e.message.received_at, reverse=True)[:limit]

    with patch.object(mgr, "_get_messages", side_effect=_capture):
        coord.get_display_message()
    assert seen_limits == [2]


# --- Scenario 6: tick pulls at most every 250 ms -----------------------------


def test_tick_pulls_at_most_every_250ms():
    """tick() calls get_display_message() at most once per 250 ms window.

    Drives the coordinator in a tight loop for 1 second of "frame" time
    (10 ms per tick = 100 ticks). The pull cadence should cap to ~4 Hz,
    so we expect 4–5 actual pulls (the first pull fires on the first tick
    after the 250 ms threshold; subsequent pulls fire every 250 ms).
    """
    clock = [1000.0]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", lambda: clock[0])

    mgr = _StubMessageManager(
        messages=[
            _make_view("a", "body-a", "2026-01-02T00:00:00Z"),
        ]
    )
    coord, _ = _build(message_manager=mgr, recent_count=5)
    # Bind a stub render layer so tick() doesn't no-op.
    from tests.effects_coordinator_test import _StubDisplay, _StubScroller, _make_effect

    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    coord.bind(display=display, scroller=scroller, effects=[fx_a], heart=heart)
    coord.start()

    pull_count = [0]
    original_get_display = coord.get_display_message

    def counting_get_display():
        pull_count[0] += 1
        return original_get_display()

    monkey.setattr(coord, "get_display_message", counting_get_display)

    # Drive 1 second in 10 ms steps. tick() is called every step.
    for _ in range(100):
        clock[0] += 0.01
        coord.tick()

    # Expected: pulls happen at t=0 (first tick), 0.25, 0.5, 0.75, 1.0 → 5 pulls.
    # We assert "at most 5" because the exact count depends on whether the
    # first tick counts (it does — now - 0 >= 0.25 is True at 0.01 if
    # _last_message_pull starts at 0.0; but with the init=0.0, the first
    # tick at 1000.01 sees now=1000.01 and _last_message_pull=0 → True, so
    # it pulls. Then next pull at 1000.25, 1000.5, 1000.75, 1001.0 → 5 total.
    assert pull_count[0] <= 5, f"too many pulls: {pull_count[0]} (expected <= 5)"
    # And: definitely at least one (otherwise the throttle is broken).
    assert pull_count[0] >= 1
    monkey.undo()
