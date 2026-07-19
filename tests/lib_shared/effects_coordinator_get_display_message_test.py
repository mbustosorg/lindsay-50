"""Tests for EffectsCoordinator.get_display_message() + the required message_manager arg.

Scenarios:
1. Required message_manager raises TypeError when omitted.
2. get_display_message returns current_message.body when a message is staged.
3. get_display_message falls back to on_deck.body when current_message is None.
4. get_display_message returns None when both slots are empty.
5. get_display_message prefers current_message over on_deck (current wins).
6. tick() picks ONLY at out→in. No pull-on-a-timer contract; the WeightedSelector
   runs at the out→in transition.

The new design (issue #26 on-deck slot refactor) replaces the legacy
random.choice + fresh-id branch logic with a pure slot reader. Picks
move to `out→in`; the buffer is only read at that transition. This
file exercises the slot-reader contract and the pick-timing contract.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import Message, MessageView

# Pull the shared stubs from the main coordinator test file so the
# render-layer shape matches the rest of the test suite.
from tests.effects_coordinator_test import (
    _StubDisplay,
    _StubScroller,
    _make_effect,
    _build as _tc_build,
)


def _make_view(message_id: str, body: str, received_at: str, suppressed: bool = False) -> MessageView:
    return MessageView(
        Message(id=message_id, sender="+1", body=body, received_at=received_at),
        source="mqtt",
        suppressed=suppressed,
    )


class _StubMessageManager:
    """Minimal MessageManager stub exposing just the surface EffectsCoordinator reads."""

    def __init__(self, messages=None, recent_count=5):
        from collections import deque
        from lib_shared.models import EffectsSettings, TextSettings

        self._entries = list(messages or [])
        self._recent_count = recent_count
        # Round 4 (queue redesign): the stub now mirrors the
        # production MessageManager API. `take_next_new_message`
        # returns None (the FIFO is empty) — this stub seeds via
        # the constructor, so all entries are "pre-existing" and
        # the random-pool path applies. Tests that exercise the
        # queue drain should append to `_new_messages_queue`
        # directly.
        self._new_messages_queue: deque = deque()
        # The coordinator reads `recent_count` (and the rest of
        # the pacing) live from `message_manager.config.effects_settings`.
        self.config = SimpleNamespace(
            effects_settings=EffectsSettings(recent_count=recent_count),
            text_settings=TextSettings(),
        )

    @property
    def messages(self):
        return SimpleNamespace(get_messages=self._get_messages)

    def _get_messages(self, limit=100, suppress=True):
        entries = list(self._entries)
        if suppress:
            entries = [e for e in entries if not getattr(e, "suppressed", False)]
        return sorted(entries, key=lambda e: e.message.received_at, reverse=True)[:limit]

    def get_messages(self, limit=100, suppress=True):
        return self._get_messages(limit, suppress)

    def get_effects_settings(self):
        return self.config.effects_settings

    def get_text_settings(self):
        return self.config.text_settings

    def add(self, view):
        self._entries.append(view)

    def take_next_new_message(self):
        try:
            return self._new_messages_queue.popleft()
        except IndexError:
            return None


def _build(message_manager=None, recent_count=5, selector=None, event_log=None):
    """Build a coordinator with the minimum layer needed to construct one."""
    if message_manager is None:
        message_manager = _StubMessageManager(recent_count=recent_count)
    else:
        message_manager.config.effects_settings.recent_count = recent_count
    coord = EffectsCoordinator(
        message_manager=message_manager,
        selector=selector,
        event_log=event_log,
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


# --- Scenario 2: get_display_message returns current_message.body ---------


def test_get_display_message_returns_current_message_body():
    """When current_message is set, get_display_message returns its body."""
    coord, _ = _build(recent_count=5)
    msg = Message(id="a", sender="+1", body="hello", received_at="2026-01-02T00:00:00Z")
    coord.current_message = msg
    coord.on_deck = Message(id="b", sender="+1", body="world", received_at="2026-01-03T00:00:00Z")
    assert coord.get_display_message() == "hello"


# --- Scenario 3: get_display_message falls back to on_deck ----------------


def test_get_display_message_returns_on_deck_when_no_current():
    """When current_message is None (e.g. intro phase), get_display_message returns on_deck.body."""
    coord, _ = _build(recent_count=5)
    coord.on_deck = Message(id="b", sender="+1", body="on-deck", received_at="2026-01-01T00:00:00Z")
    assert coord.current_message is None
    assert coord.get_display_message() == "on-deck"


# --- Scenario 4: get_display_message returns None on empty slots ----------


def test_get_display_message_returns_none_when_no_slots():
    """Both slots None → get_display_message returns None."""
    coord, _ = _build(recent_count=5)
    assert coord.current_message is None
    assert coord.on_deck is None
    assert coord.get_display_message() is None


# --- Scenario 5: get_display_message prefers current over on_deck --------


def test_get_display_message_prefers_current_over_on_deck():
    """current_message wins when both slots are set (the body being shown now,
    not what's queued for next). Setting on_deck alone returns on_deck.body;
    setting both returns current.body."""
    coord, _ = _build(recent_count=5)
    coord.on_deck = Message(id="b", sender="+1", body="queued", received_at="2026-01-01T00:00:00Z")
    coord.current_message = Message(id="a", sender="+1", body="now", received_at="2026-01-02T00:00:00Z")
    assert coord.get_display_message() == "now"
    coord.current_message = None
    assert coord.get_display_message() == "queued"


# --- Scenario 6: tick() picks ONLY at out→in -----------------------------


def test_tick_picks_at_out_to_in_only():
    """tick() runs the WeightedSelector ONLY at the out→in transition.

    Drives the coordinator through several mode transitions and counts
    `_pick_message_via_selector` calls. The new design has picks at:
      - intro→out (seeds `on_deck` for the first cycle)
      - each out→in (consumes `on_deck` for `current_message`, seeds next `on_deck`)

    Background→out does NOT pull (a side effect of moving the pick to
    out→in). Pull-on-a-timer would produce a much higher count; this
    test pins the new contract.
    """
    clock = [1000.0]
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", lambda: clock[0])

    msgs = [_make_view(f"id{i:02d}", f"body-{i:02d}", f"2026-01-{10 + i:02d}T00:00:00Z") for i in range(5)]
    mgr = _StubMessageManager(messages=msgs)
    # Tight pacing so multiple cycles fit in 2 seconds of clock time.
    mgr.config.effects_settings.intro_seconds = 0.0
    mgr.config.effects_settings.fade_seconds = 0.05
    mgr.config.effects_settings.hold_seconds = 0.05
    mgr.config.effects_settings.text_out_seconds = 0.05
    # IDLE_SECONDS_AFTER_HOLD is a module constant (3.0 default); the
    # test patches it down so background→out → out→in cycles complete
    # within the 2-second window. The new design ignores settings.toml
    # idle_seconds here — that's the behavioral knob move.
    monkey.setattr("lib_shared.effects_coordinator.IDLE_SECONDS_AFTER_HOLD", 0.05)
    coord, _ = _build(message_manager=mgr, recent_count=5)

    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    coord.bind(display=display, scroller=scroller, effects=[fx_a], heart=heart)
    coord.start()

    pick_count = [0]
    original_pick = coord._pick_message_via_selector

    def counting_pick(*args, **kwargs):
        # The coordinator calls `_pick_message_via_selector(exclude_id=...)`
        # at the out→in transition (anti-repeat hint); the bare call at
        # intro→out passes no kwargs. Forward transparently so the
        # signature stays in sync with the call site — the assertion
        # below only cares about call COUNT, not arguments.
        pick_count[0] += 1
        return original_pick(*args, **kwargs)

    monkey.setattr(coord, "_pick_message_via_selector", counting_pick)

    # Drive 2 seconds in 10 ms steps. tick() is called every step.
    for _ in range(200):
        clock[0] += 0.01
        coord.tick()

    # Lower bound: ≥ 2 picks (intro→out seed + first out→in consume +
    # subsequent out→in picks). Upper bound: < 50. The pull-on-a-timer
    # pattern would produce ~120 picks at 250 ms throttle × 2 s; the
    # test pins the new contract by asserting a small count that
    # reflects "one pick per out→in transition".
    assert pick_count[0] >= 2, f"expected ≥ 2 picks (intro→out seed + at least one out→in); got {pick_count[0]}"
    assert (
        pick_count[0] < 50
    ), f"too many picks: {pick_count[0]}. The pull-on-a-timer pattern is back if this exceeds ~50."
    monkey.undo()
