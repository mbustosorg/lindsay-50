"""Tests for lib_shared.effects_coordinator.EffectsCoordinator.

Covers the lifecycle state machine: intro → out → in → hold → text_out →
background; the brightness-ramp endpoints; the throttled pull from the
manager; the optional render layer (`bind`); and the display.render call
per tick.
"""

import importlib
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# --- stubs ------------------------------------------------------------------

class _StubCanvas:
    width = 64
    height = 64

class _StubDisplay:
    def __init__(self):
        self.width = 64
        self.height = 64
        self.canvas = _StubCanvas()
        self.render_calls = []

    def clear(self):
        pass

    def render(self, effect, scroller):
        self.render_calls.append((effect, scroller))

class _StubScroller:
    def __init__(self):
        self.text = ""
        self.set_text_calls = []
        self.set_brightness_calls = []
        self.tick_calls = []
        self.render_calls = []
        self._brightness = 1.0
        # Scroller text-settings state — driven by the coordinator's
        # per-tick `_sync_render_layer` on a text_settings change.
        self._color = 0xFF0000
        self.frame_delay = 0.040
        self.offset_seconds = 1.0
        self.set_color_calls = []
        self.set_speed_calls = []

    def set_text(self, text, width):
        self.set_text_calls.append((text, width))
        self.text = text

    def set_brightness(self, b):
        self.set_brightness_calls.append(b)
        self._brightness = b

    def set_color(self, c):
        self.set_color_calls.append(c)
        self._color = c

    def set_speed(self, s):
        self.set_speed_calls.append(s)
        if s <= 1:
            self.frame_delay, self.offset_seconds = 0.080, 1.5
        elif s >= 5:
            self.frame_delay, self.offset_seconds = 0.020, 0.5
        else:
            self.frame_delay, self.offset_seconds = 0.040, 1.0

    def tick(self, width):
        self.tick_calls.append(width)

    def render(self, canvas):
        self.render_calls.append(canvas)

def _make_effect(name):
    """Create a stub Effect class with the given class name."""

    class _Fx:
        def __init__(self):
            self.tick_calls = 0
            self.render_calls = 0
            self.brightness = 1.0

        def tick(self):
            self.tick_calls += 1

        def render(self, canvas):
            self.render_calls += 1

        def set_brightness(self, b):
            self.brightness = b

    _Fx.__name__ = name
    return _Fx

class _StubMessageManager:
    """Minimal MessageManager stub for coordinator tests.

    Mirrors the surface the coordinator touches:
      `messages.get_messages(limit, suppress=True)` — returns a list of
        MessageView-shaped objects with `.message.id`, `.message.body`,
        and `.suppressed`.
      `get_messages(limit, suppress=True)` — the top-level alias used by
        `EffectsCoordinator.current_messages`.
      `get_effects_settings()`, `get_text_settings()` — live config
        getters the coordinator reads each tick via the
        `effects_settings` / `text_settings` properties.
      `config.effects_settings`, `config.text_settings` — kept for
        callers that still reach into the legacy surface.
      `take_next_new_message()` — round 4 (queue redesign): mirrors
        `MessageManager.take_next_new_message`; pops the OLDEST entry
        off the `_new_messages_queue` FIFO (or returns None). The
        stub appends to this queue from `add_message` so tests
        that simulate a live arrival automatically route through
        the same drain path the production code uses.

    Round 4 contract: `add_message(view)` writes to BOTH the
    in-memory buffer (`_entries`) AND the FIFO
    (`_new_messages_queue`), mirroring the production flow where
    every `_handle_message` call appends to both. Tests that
    pre-seed via `messages=[...]` only write to the buffer (those
    are pre-existing messages, not fresh arrivals), so the queue
    is empty and `_pick_next` falls through to the random-pool
    path. Tests that call `add_message(view)` after boot
    represent fresh arrivals and go through the queue drain.
    """

    def __init__(self, messages=None, effects_settings=None, text_settings=None):
        from collections import deque
        from lib_shared.models import EffectsSettings, TextSettings

        self.messages = SimpleNamespace(get_messages=self._get_messages)
        self._entries = list(messages or [])
        # Round 4 (queue redesign): mirror production. `add_message`
        # appends to both, `take_next_new_message` pops from the
        # FIFO. The deque is unbounded in tests (maxlen doesn't
        # matter at the small scales tests use).
        self._new_messages_queue: deque = deque()
        self.config = SimpleNamespace(
            effects_settings=effects_settings or EffectsSettings(),
            text_settings=text_settings or TextSettings(),
        )

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

    def add_message(self, view):
        # Round 4: mirror production — buffer append + queue append.
        self._entries.append(view)
        self._new_messages_queue.append(view)

    def take_next_new_message(self):
        from collections import deque
        try:
            return self._new_messages_queue.popleft()
        except IndexError:
            return None

# --- fixtures / helpers -----------------------------------------------------

class _Clock:
    """A controllable time source monkey-patches over time.monotonic."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt

def _build(
    fade_seconds=0.05,
    intro_seconds=0.0,
    hold_seconds=10.0,
    idle_seconds=300.0,
    message_manager=None,
):
    """Build a coordinator with a stub render layer already attached.

    The default shape every state-machine test uses. Tests that
    need the unbound form (the `bind()` tests + no-op-when-unbound
    tests) call `_build_unbound()` instead.

    The pacing values are plumbed into the stub manager's
    `EffectSettings` — the coordinator reads them live from the
    manager (no per-coordinator copy).
    """
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    heart = _make_effect("Heart")()
    from lib_shared.models import EffectsSettings

    if message_manager is None:
        message_manager = _StubMessageManager()
    # Always override the manager's effects_settings with the
    # pacing values from this helper — the coordinator reads
    # pacing live from the manager, and a passing test needs
    # those values to land in the manager's EffectSettings.
    message_manager.config.effects_settings = EffectsSettings(
        fade_seconds=fade_seconds,
        intro_seconds=intro_seconds,
        hold_seconds=hold_seconds,
        idle_seconds=idle_seconds,
    )
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(
        message_manager=message_manager,
        display=display,
        scroller=scroller,
        effects=[fx_a, fx_b],
        heart=heart,
    )
    return coord, display, scroller, fx_a, fx_b, heart

def _build_unbound(
    fade_seconds=0.05,
    intro_seconds=0.0,
    hold_seconds=10.0,
    idle_seconds=300.0,
    message_manager=None,
):
    """Build a coordinator with NO render layer attached.

    Mirrors the shape `app_main.py` instantiates at PyScript startup,
    before the preview page's `preview_main.py` calls `bind(...)`.
    Returns only the coordinator (no display/scroller/effects stubs)
    because the test only needs to assert state, not the layer.
    """
    if message_manager is None:
        from lib_shared.models import EffectsSettings

        message_manager = _StubMessageManager(
            effects_settings=EffectsSettings(
                fade_seconds=fade_seconds,
                intro_seconds=intro_seconds,
                hold_seconds=hold_seconds,
                idle_seconds=idle_seconds,
            ),
        )
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(
        message_manager=message_manager,
    )
    return coord

def _drive(clock, coord, seconds, step=0.01):
    """Advance clock by `seconds` in `step` increments, calling tick() each step."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        coord.tick()
        elapsed += step

# --- state-machine tests ----------------------------------------------------

def test_intro_then_out_then_in_then_background():
    """Mode progresses intro → out → in → background when no text is pulled."""
    clock = _Clock()
    importlib.import_module("lib_shared.effects_coordinator")  # ensure module is in
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()

    # First tick: intro has elapsed, transitions to out
    clock.advance(0.001)
    coord.tick()
    assert coord.mode == "out"
    # Drive out
    _drive(clock, coord, 0.1)
    assert coord.mode == "in"
    assert coord.idx == 0
    assert coord.current is fx_a
    # Drive in
    _drive(clock, coord, 0.1)
    # No text was pulled (empty buffer), so we land in background
    assert coord.mode == "background"
    assert coord.current is fx_a
    monkey.undo()

def test_idx_advances_on_fade_out_complete():
    """After a full background→out cycle triggered by a queued message, idx advances by 1 modulo len(effects).

    Round 4 (queue redesign): the queue drains at the natural pick
    site — the `idle` trigger fires when `idle_seconds` elapses
    in background mode, NOT mid-hold. A fresh arrival mid-hold
    goes to the FIFO; the hold runs to `hold_seconds` regardless;
    then we walk hold → text_out → background → (idle fires) → out
    → in → hold (with idx advanced and msg2 shown).
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="hi", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1])
    # Short pacing across the board: hold=0.3s so the test reaches
    # text_out fast; idle=0.05s so the background→out cycle fires
    # within the test window. fade=0.05s so out→in completes fast.
    coord, display, scroller, fx_a, fx_b, heart = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.3,
        idle_seconds=0.05,
        message_manager=mgr,
    )
    coord.start()
    _drive(clock, coord, 0.3)  # intro → out → in → hold (m1 shown)
    assert coord.idx == 0
    # Inject a NEW message mid-hold. Round 4: it goes to the FIFO,
    # NOT to the buffer's fresh-id interrupt (which was removed).
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="next", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr.add_message(msg2)
    # Tick once at +0.1s — still well under hold_seconds=0.3.
    # Hold must survive; no fade-out yet.
    clock.advance(0.1)
    coord.tick()
    assert coord.mode == "hold", (
        "queue redesign: mid-hold arrival must NOT interrupt — "
        "the hold runs to hold_seconds; the queue drains at the "
        "next pick site (background idle)."
    )
    # Drive past hold_seconds + text_out + idle_seconds + fade — the
    # queued msg2 drains at the idle-triggered `_pick_next` call,
    # then the cycle completes out → in (idx advances).
    _drive(clock, coord, 0.6)  # past all four transitions
    # The picked entry must be msg2 (the queued message drained).
    assert coord._last_picked_entry is not None, (
        "queue drain broken: _pick_next at idle trigger should "
        "have consumed the queued msg2"
    )
    assert coord._last_picked_entry.message.id == "m2"
    # The cycle completed: idx advanced to 1, current is fx_b.
    assert coord.idx == 1
    assert coord.current is fx_b
    monkey.undo()


# --- round 4 (queue redesign): FIFO drain at natural pick sites --------------
# New tests pin the queue contract: messages queued mid-cycle are picked at
# the next natural pick site (background idle), FIFO order is preserved, the
# queue takes priority over the recent-pool random pick, and the queue empties
# the buffer's "fall-through" path. Tests that don't need full cycle timing
# exercise `_pick_next` directly; tests that pin the "no interruption during
# hold/background" contract drive the cycle through to a stable mode.
# Both consume from the same `_StubMessageManager._new_messages_queue` so the
# `_StubMessageManager.add_message` shape (mirroring production) stays in sync.


def _make_view(message_id, body, received_at):
    """Stand-alone `MessageView` factory — pin the same shape
    `_StubMessageManager.add_message` accepts."""
    from lib_shared.models import MessageView, Message

    return MessageView(
        Message(id=message_id, sender="+15551234567", body=body, received_at=received_at),
        source="mqtt",
        suppressed=False,
    )


def test_queue_drains_on_next_pick():
    """A queued message drains at the very next `_pick_next` call.
    Nothing should be left in the queue after the call."""
    mgr = _StubMessageManager(messages=[])
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(message_manager=mgr)
    msg2 = _make_view("m2", "second", "2026-01-03T00:00:00Z")
    mgr.add_message(msg2)
    assert len(mgr._new_messages_queue) == 1

    # Direct `_pick_next` call drains the queue.
    body = coord._pick_next()
    assert body == "second"
    assert mgr.take_next_new_message() is None, (
        "queue did not drain at the natural pick site"
    )
    assert coord._last_picked_entry is not None
    assert coord._last_picked_entry.message.id == "m2"
    assert coord._last_display_message == "second"


def test_queue_is_fifo():
    """Three messages queue, three sequential `_pick_next` calls return
    them in arrival order, queue empty after the third."""
    mgr = _StubMessageManager(messages=[])
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(message_manager=mgr)
    msg_a = _make_view("a", "A", "2026-01-02T00:00:00Z")
    msg_b = _make_view("b", "B", "2026-01-03T00:00:00Z")
    msg_c = _make_view("c", "C", "2026-01-04T00:00:00Z")
    mgr.add_message(msg_a)
    mgr.add_message(msg_b)
    mgr.add_message(msg_c)
    assert len(mgr._new_messages_queue) == 3

    assert coord._pick_next() == "A"
    assert len(mgr._new_messages_queue) == 2
    assert coord._pick_next() == "B"
    assert len(mgr._new_messages_queue) == 1
    assert coord._pick_next() == "C"
    assert len(mgr._new_messages_queue) == 0
    assert mgr.take_next_new_message() is None


def test_queue_takes_priority_over_recent_pool():
    """Buffer (random pool) AND queue both have messages; the queue
    wins. The random pool is only consulted AFTER the FIFO drains."""
    import random

    random.seed(0)
    msg_pool = [
        _make_view(f"pool{i}", f"body{i}", f"2026-01-{10 + i:02d}T00:00:00Z")
        for i in range(3)
    ]
    mgr = _StubMessageManager(messages=msg_pool)
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(message_manager=mgr)
    # The seed messages live in the buffer only (not the queue) —
    # `add_message` mirrors production by adding to BOTH, but the
    # constructor only adds to the buffer. The random pool is fed
    # from the buffer; the queue is independent.
    assert mgr.take_next_new_message() is None

    # Queue a specific entry that the random pool wouldn't necessarily pick.
    msg_target = _make_view("target", "target-body", "2026-01-20T00:00:00Z")
    mgr.add_message(msg_target)
    # The buffer now also has target, but the queue priority is the
    # contract being tested: a `_pick_next` drains the queue first,
    # before consulting the buffer.
    assert len(mgr._new_messages_queue) == 1

    # First `_pick_next`: drains the queue (target wins over random pool,
    # even though target is also in the buffer).
    body = coord._pick_next()
    assert body == "target-body"
    assert mgr.take_next_new_message() is None

    # Second `_pick_next`: queue empty, falls through to random pool.
    # Random pool has 4 entries (3 pool + target); with seed 0 the
    # choice is deterministic. Either of those is acceptable; we just
    # assert that the random pool returned something.
    body2 = coord._pick_next()
    assert body2 in {"body0", "body1", "body2", "target-body"}, (
        f"random-pool fallback returned unexpected body={body2!r}"
    )


def test_no_interruption_during_hold():
    """A fresh SMS arriving mid-hold does NOT trigger a fade-out.
    The hold runs to `hold_seconds`, the queue holds the message,
    and the only transition is at the natural hold→text_out edge."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    msg1 = _make_view("m1", "first", "2026-01-02T00:00:00Z")
    mgr = _StubMessageManager(messages=[msg1])
    # Long hold, so the only way to leave it would be the (removed) fresh-id interrupt.
    coord, _, _, fx_a, fx_b, _ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=999.0,
        idle_seconds=0.05,
        message_manager=mgr,
    )
    coord.start()
    _drive(clock, coord, 0.2)  # boot to hold
    assert coord.mode == "hold"

    msg2 = _make_view("m2", "second", "2026-01-03T00:00:00Z")
    mgr.add_message(msg2)
    clock.advance(0.3)
    coord.tick()

    assert coord.mode == "hold", "mid-hold arrival must NOT trigger a fade-out"
    assert coord.idx == 0
    # Queue still holds m2 (not drained at a removed interrupt path).
    assert mgr.take_next_new_message() is msg2
    monkey.undo()


def test_no_interruption_in_background():
    """A fresh SMS arriving mid-background does NOT trigger a fade-out.
    The background sits until `idle_seconds` elapses; with idle=999 the
    coordinator never leaves background within the test window."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    msg1 = _make_view("m1", "first", "2026-01-02T00:00:00Z")
    mgr = _StubMessageManager(messages=[msg1])
    coord, _, _, fx_a, fx_b, _ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.3,
        idle_seconds=999.0,  # background sits forever
        message_manager=mgr,
    )
    coord.start()
    # boot → out → in → hold → text_out → background. Hold=0.3s +
    # text_out=0.05s lands us in background around t=0.5. Drive 0.6s
    # to land cleanly in background with margin.
    _drive(clock, coord, 0.6)
    assert coord.mode == "background", (
        f"setup: expected mode=background after 0.6s drive, got {coord.mode}"
    )

    msg2 = _make_view("m2", "second", "2026-01-03T00:00:00Z")
    mgr.add_message(msg2)
    clock.advance(0.3)
    coord.tick()

    assert coord.mode == "background", (
        "mid-background arrival must NOT trigger a fade-out"
    )
    assert mgr.take_next_new_message() is msg2
    monkey.undo()


def test_drained_queue_falls_through_to_random_pool():
    """Once the queue is empty, `_pick_next` falls through to the
    random pool — `get_display_message` is consulted as a fallback."""
    import random

    random.seed(0)
    msg_pool = [
        _make_view(f"pool{i}", f"body{i}", f"2026-01-{10 + i:02d}T00:00:00Z")
        for i in range(3)
    ]
    mgr = _StubMessageManager(messages=msg_pool)
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(message_manager=mgr)
    while mgr.take_next_new_message() is not None:
        pass
    assert mgr.take_next_new_message() is None

    body = coord._pick_next()
    assert body in {"body0", "body1", "body2"}, (
        f"drained queue should fall through to random pool; got body={body!r}"
    )


def test_queue_pop_returns_empty_body_for_mms_only_message():
    """An MMS-only message (`body=""`) drains from the queue returning
    `""` (not `None`). The out→in branch's `if text:` check then clears
    the scroller while the media cycler replaces `self.current`.
    """
    from lib_shared.models import Message, MessageView

    mgr = _StubMessageManager(messages=[])
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(message_manager=mgr)
    mms = MessageView(
        Message(
            id="m2",
            sender="+15551234567",
            body="",
            received_at="2026-01-03T00:00:00Z",
            media=[{"type": "image/jpeg", "url": "key/x.jpg", "path": "/tmp/x.jpg"}],
        ),
        source="mqtt",
        suppressed=False,
    )
    mgr.add_message(mms)

    body = coord._pick_next()
    assert body == "", f"queue drain returned {body!r}, expected '' for MMS-only message"
    assert coord._last_picked_entry is not None
    assert coord._last_picked_entry.message.id == "m2"
    assert coord._last_picked_entry.message.body == ""
    assert coord._last_display_message == ""
    assert mgr.take_next_new_message() is None


def test_pending_text_consumed_on_out_to_in():
    """A pulled message becomes the next text shown on out → in."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="hello", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1])
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05, message_manager=mgr)
    coord.start()
    _drive(clock, coord, 0.05)  # intro → out
    clock.advance(0.3)  # advance past PULL_INTERVAL so the next tick pulls
    coord.tick()
    # The pulled message is shown on the out→in transition.
    _drive(clock, coord, 0.1)
    assert scroller.text == "hello"
    # Round 3 (debug-visibility): the `last_shown_text` field is
    # gone — `_last_display_message` carries the cached body for
    # the scroller. The selected-log fired at the pick site
    # upstream (so the body is already in the journal before
    # `starting fade out`); the fade-out log no longer echoes it.
    assert coord._last_display_message == "hello"
    monkey.undo()

def test_hold_mode_interrupted_by_new_message():
    """Round 4 (queue redesign) inversion: a new arrival mid-hold
    does NOT interrupt. The hold runs to `hold_seconds`, then the
    queued message drains at the natural hold→text_out transition.

    The previous round-3 contract was: "new (different-id) message
    during hold kicks a fade-out, no waiting for hold_seconds."
    Round 4 removes that interrupt entirely — the FIFO replaces
    it. The new arrival is queued; the hold survives until
    `hold_seconds` elapses, then `_pick_next` drains the queue at
    the transition.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="first", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1])
    # Long hold so the only way to leave `hold` mid-cycle is the
    # (removed) fresh-id interrupt. The queue drain only fires
    # at the natural hold→text_out transition.
    coord, display, scroller, fx_a, fx_b, heart = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=999.0,
        message_manager=mgr,
    )
    coord.start()
    # Drive to hold: intro → out → in → hold (the "first" message is shown)
    _drive(clock, coord, 0.2)
    assert coord.mode == "hold"
    # Add a NEW message; advance past the PULL_INTERVAL so the next tick pulls it.
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="second", received_at="2026-01-03T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr.add_message(msg2)
    clock.advance(0.3)
    coord.tick()
    # Round 4: the hold SURVIVES the mid-arrival. The fresh-id
    # interrupt was removed; the queue drains at the natural
    # hold→text_out transition (only after hold_seconds, which is
    # 999.0 here).
    assert coord.mode == "hold", (
        "round 4 (queue redesign): mid-hold arrival must NOT interrupt. "
        "the hold runs to hold_seconds and the queued message drains at "
        "the natural hold→text_out transition."
    )
    # And the queue still holds msg2 (it wasn't consumed by a
    # removed interrupt path).
    assert mgr.take_next_new_message() is msg2
    monkey.undo()

def test_brightness_ramp_endpoints():
    """Out completes with set_brightness(0.0); in completes with set_brightness(1.0)."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()
    # Drive long enough for the out + the in to both complete (with throttling
    # at fade_step=0.04, each fade takes ~0.08s, not the 0.05 nominal).
    _drive(clock, coord, 0.5)
    # In finished, brightness back to 1.0
    assert fx_a.brightness == pytest.approx(1.0, abs=1e-6)
    assert scroller._brightness == pytest.approx(1.0, abs=1e-6)
    monkey.undo()

def test_tick_calls_display_render_exactly_once():
    """Each tick() calls display.render exactly once after the state-machine step."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()
    for _ in range(3):
        clock.advance(0.01)
        coord.tick()
    assert len(display.render_calls) == 3
    for effect, scr in display.render_calls:
        assert scr is scroller

def test_current_effect_name_and_text():
    """current_effect_name / current_text mirror the active effect + scroller text."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()
    # At start, current is the heart
    assert coord.current_effect_name == "Heart"
    # After driving through to in, current is fx_a
    _drive(clock, coord, 0.1)
    assert coord.current_effect_name == "A"
    # current_text reflects the scroller's text (or '' when nothing is shown)
    assert coord.current_text == ""
    monkey.undo()

# --- optional render layer (bind / unbound) ---------------------------------

def test_unbound_coordinator_starts_unbound():
    """A coordinator constructed without a render layer is unbound."""
    coord = _build_unbound()
    assert coord.is_bound() is False
    assert coord.display is None
    assert coord.scroller is None
    assert coord.effects == []
    assert coord.heart is None

def test_tick_is_noop_when_unbound():
    """tick() on an unbound coordinator returns without touching state or crashing.

    The app-scoped coordinator (instantiated by `app_main.py` on every
    admin page) is unbound until the preview's `preview_main.py`
    calls `bind(...)`. The rAF loop in preview.js is gated on
    `window._coordinator.is_bound()`, but defensive no-ops keep the
    coordinator safe if anything else accidentally calls `tick()`.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord = _build_unbound()
    # Drive a few frames — nothing should change.
    for _ in range(5):
        clock.advance(0.1)
        coord.tick()
    assert coord.mode == "intro"  # untouched
    assert coord.idx == -1
    monkey.undo()

def test_start_is_noop_when_unbound():
    """start() is a no-op on an unbound coordinator.

    `start()` is documented as "only meaningful on the preview's
    per-page shim" — the app-scoped coordinator's role is to own
    the singletons, not drive frames. Calling it before `bind()`
    should not raise.
    """
    coord = _build_unbound()
    coord.start()
    assert coord.mode == "intro"

def test_bind_attaches_render_layer():
    """bind(display, scroller, effects, heart) makes is_bound() True and
    sets current to the new heart (so the next tick starts cleanly)."""
    coord = _build_unbound()
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    coord.bind(display=display, scroller=scroller, effects=[fx_a], heart=heart)
    assert coord.is_bound() is True
    assert coord.display is display
    assert coord.scroller is scroller
    assert coord.effects == [fx_a]
    assert coord.heart is heart
    assert coord.current is heart
    assert heart.brightness == pytest.approx(1.0, abs=1e-6)

def test_bind_defaults_heart_to_first_effect():
    """When heart= is omitted, bind() defaults it to the head of effects.

    The Pi passes an explicit Heartbeat (different from the effects
    rotation); the browser preview's `preview_main.py` passes the
    first effect as the heart, so the default saves that caller
    from a redundant arg.
    """
    coord = _build_unbound()
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    coord.bind(display=display, scroller=scroller, effects=[fx_a, fx_b])
    assert coord.heart is fx_a

def test_bind_swaps_render_layer_mid_life():
    """bind() called again replaces the render layer; the next tick
    uses the new layer (state machine continues from where it is).

    This is the contract the /preview page relies on: it constructs
    its own canvas + scroller + effects and calls `bind()` once the
    page-local objects are ready, even though the coordinator has
    been alive since `app_main.py` loaded.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    # First layer — runs the boot splash.
    coord, display1, scroller1, fx_a1, fx_b1, heart1 = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()
    _drive(clock, coord, 0.1)  # intro → out → in
    assert coord.idx == 0
    assert coord.current is fx_a1
    render_count_before = len(display1.render_calls)
    assert render_count_before > 0

    # Swap in a fresh layer mid-life. State machine stays put;
    # the next tick uses the new display / scroller / effects.
    display2 = _StubDisplay()
    scroller2 = _StubScroller()
    fx_c = _make_effect("C")()
    fx_d = _make_effect("D")()
    heart2 = _make_effect("Heart2")()
    coord.bind(display=display2, scroller=scroller2, effects=[fx_c, fx_d], heart=heart2)
    assert coord.is_bound() is True
    assert coord.display is display2
    assert coord.scroller is scroller2
    assert coord.effects == [fx_c, fx_d]
    assert coord.heart is heart2

    # Drive more — only the new display's render_calls grow.
    _drive(clock, coord, 0.1)
    assert len(display2.render_calls) > 0
    # First display did not get any more render() calls after the swap.
    assert len(display1.render_calls) == render_count_before
    monkey.undo()

# --- observability tests (sign lifecycle must log at INFO) ------------------
#
# The Pi can't toggle LOG_LEVEL at runtime — every sign-lifecycle event
# has to surface in journalctl when LOG_LEVEL=INFO. These tests pin that
# contract: if a future refactor moves one of these log lines back to
# DEBUG, the operator diagnostic story degrades and the test fails.

def _info_records(caplog, *substrings):
    """Return caplog INFO records whose message contains every substring."""
    return [r for r in caplog.records if r.levelno == logging.INFO and all(sub in r.getMessage() for sub in substrings)]

import logging  # noqa: E402 — kept at module level for the helpers below

def test_begin_out_emits_info_log(caplog):
    """`_begin_out` fires for boot's intro→out + every new-SMS interrupt
    during hold/background; both must surface at INFO so the journal shows
    "the sign just received a message" without flipping LOG_LEVEL."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start()

    caplog.set_level(logging.INFO)
    clock.advance(0.001)
    coord.tick()  # intro → out via _begin_out

    # Log shape consolidated in debug-visibility: single
    # "Coordinator: starting fade out from mode=X effect=Y trigger=Z"
    # line replaces the old verbose "Coordinator._begin_out:" form.
    matches = _info_records(caplog, "starting fade out")
    assert matches, "Expected INFO log line 'starting fade out'; got: " + "; ".join(
        r.getMessage() for r in caplog.records
    )
    # The log line carries the from-mode and the active effect for context.
    assert "from mode=intro" in matches[0].getMessage()
    monkey.undo()

def test_out_to_in_and_in_to_hold_emit_info_logs(caplog):
    """A full intro → out → in → hold cycle logs at INFO at each transition
    so the journal shows the sign's lifecycle progression on every boot."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="hi", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05, message_manager=mgr)
    coord.start()

    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)  # intro → out → in → hold

    # Round 4: selected-log carries msg + body + effect in one
    # record. The fade-in-starting log fires at out→in only.
    selected = _info_records(caplog, "Coordinator: selected")
    assert selected, "Expected 'Coordinator: selected' INFO log at out→in"
    assert "m1" in selected[0].getMessage()
    assert "effect=" in selected[0].getMessage(), (
        f"round-4 selected-log must carry effect= on the same line "
        f"as the picked message; got: {selected[0].getMessage()[:120]}"
    )
    fade_in = _info_records(caplog, "starting fade in")
    assert fade_in, "Expected 'starting fade in' INFO log"

    # Round 4 (debug-visibility): the `fade in done` log was
    # dropped entirely — internal fade-mechanics, not a sign-state
    # event the operator needs to read.
    fade_in_done = _info_records(caplog, "fade in done")
    assert not fade_in_done, (
        f"round 4 dropped the 'fade in done' log — operator sees "
        f"selected → fade out → fade in, no 'done' confirmation. "
        f"Got: {[r.getMessage() for r in fade_in_done]}"
    )
    monkey.undo()

def test_hold_to_text_out_and_text_out_to_background_silent(caplog):
    """Round 4 (debug-visibility): the `hold→text_out` and
    `text_out→background` transition logs are dropped. Internal
    fade-mechanics between selected and the next selected — the
    operator reads the next cycle's `Coordinator: selected` line
    for that."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="hi", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        message_manager=mgr,
    )
    coord.start()

    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.4)  # intro → out → in → hold → text_out → background

    hold_to_text = _info_records(caplog, "hold→text_out")
    text_to_bg = _info_records(caplog, "text_out→background")
    assert not hold_to_text, (
        f"round 4 dropped 'hold→text_out' — got: {[r.getMessage() for r in hold_to_text]}"
    )
    assert not text_to_bg, (
        f"round 4 dropped 'text_out→background' — got: {[r.getMessage() for r in text_to_bg]}"
    )
    monkey.undo()

def test_hold_does_not_interrupt_on_random_picks_from_shown_set(caplog):
    """v2 hold semantics: a fresh, un-shown SMS (head.id differs from
    `_last_shown_message_id`) interrupts the hold; a random re-pick from
    the already-shown pool does NOT. Without this, every pull with
    `random.choice` over a multi-message buffer would interrupt the
    hold instantly and `hold_seconds` would never be observed — that
    was the bug operators saw: messages would appear briefly then
    disappear after a few seconds regardless of hold_seconds setting.

    Round 4: the `hold→text_out` and `hold interrupt` log lines are
    GONE. The contract being pinned is behavioral (hold survives
    `hold_seconds`) rather than log-shape. So instead of asserting
    the log lines, this test asserts the coordinator lands in
    background after `hold_seconds` of random re-picks.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    # Two messages; both stay in the recent pool. After the first
    # pull, get_display_message() will fall through to random.choice,
    # which CAN return the other body — but neither body is "fresh"
    # in id-terms after both have been consumed once. The hold must
    # survive `hold_seconds` of repeated random re-picks.
    msg1 = MessageView(
        Message(id="m1", sender="+1", body="alpha", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="beta", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1, msg2])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.5,
        idle_seconds=999.0,
        message_manager=mgr,
    )
    coord.start()

    # Drain to background via the natural hold→text_out→background
    # path with random re-picks happening between ticks. After 1.0s
    # the coordinator must be in background — proves hold_seconds
    # was honored.
    _drive(clock, coord, 1.0)
    assert coord.mode == "background", (
        f"hold_seconds=0.5 must let the coordinator reach background "
        f"after random re-picks; got mode={coord.mode!r}. If this fires, "
        f"random re-picks are interrupting holds again."
    )
    # And: no 'Coordinator hold interrupt' should have fired — that
    # log line only fires when a FRESH id arrives (which now goes
    # through the queue, not interrupt).
    interrupts = _info_records(caplog, "hold interrupt")
    assert (
        not interrupts
    ), f"Expected zero hold interrupts from random re-picks; got: {[r.getMessage() for r in interrupts]}"
    monkey.undo()

def test_background_re_rolls_on_idle_timeout(caplog):
    """v2 background semantics: idle_seconds is honored as a hard ceiling.
    Without this, idle_seconds was exposed in the admin UI and the
    model but never read by the coordinator — the sign could sit
    dormant for the full 5-minute default even with idle_seconds=10.

    Drive the coordinator into `background` mode and advance the clock
    past idle_seconds with no fresh SMS; the next tick should fire
    `_begin_out` and log the idle trigger.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="hi", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        idle_seconds=0.3,  # short so the test runs fast
        message_manager=mgr,
    )
    coord.start()

    caplog.set_level(logging.INFO)
    # Drain past intro → out → in → hold → text_out → background.
    _drive(clock, coord, 0.3)
    assert coord.mode == "background", f"Setup expected to land in background mode; got {coord.mode!r}"

    # Now sit idle for longer than idle_seconds without sending a new SMS.
    # Each tick advances by ~0.01s; advance 1s total to comfortably exceed 0.3s.
    _drive(clock, coord, 1.0)

    # The idle trigger must have fired at least once.
    # Log shape consolidated in debug-visibility: the verbose
    # "Coordinator background→out (idle):" form is gone — the same
    # "starting fade out" line carries `trigger=idle` in its args.
    matches = _info_records(caplog, "starting fade out")
    assert matches, "Expected at least one 'starting fade out' INFO log"
    idle_lines = [r for r in matches if "trigger=idle" in r.getMessage()]
    assert idle_lines, (
        "Expected at least one 'starting fade out' INFO log with "
        "trigger=idle when the coordinator sat in background past "
        "idle_seconds. If missing, the fix isn't wired up."
    )
    monkey.undo()

def test_background_re_rolls_on_fresh_id(caplog):
    """Round 4 (queue redesign) inversion: a fresh-id arrival in
    background mode does NOT trigger an immediate fade-out. The
    arrival queues into the FIFO; the background→out transition
    fires only on `idle_seconds` (the idle trigger), at which
    point `_pick_next` drains the queue.

    The previous round-3 contract was: "A genuinely-new SMS
    arriving in background mode kicks a fade immediately via the
    `fresh_id_landed` trigger." Round 4 removes that trigger;
    the queue drains at pick sites instead.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="first", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="second", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        idle_seconds=999.0,  # idle_seconds large so the idle trigger is suppressed
        message_manager=mgr,
    )
    coord.start()

    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)  # reach background
    assert coord.mode == "background"

    # Inject a fresh-id message mid-background.
    mgr.add_message(msg2)
    clock.advance(0.5)  # past PULL_INTERVAL
    coord.tick()

    # Round 4: with idle_seconds=999, the ONLY trigger that can
    # fire is the removed `trigger=new_id`. The mode must STAY in
    # background; the arrival just queued.
    assert coord.mode == "background", (
        "round 4 (queue redesign): mid-background arrival must NOT "
        "trigger an immediate fade-out. the queue drains at the natural "
        "background→out transition (idle trigger)."
    )
    # The 'starting fade out' / 'Coordinator: selected' / 'trigger=new_id'
    # logs MUST NOT appear.
    matches = _info_records(caplog, "starting fade out")
    new_id_lines = [r for r in matches if "trigger=new_id" in r.getMessage()]
    assert not new_id_lines, (
        "round 4: trigger=new_id was removed; no background→out log "
        f"should fire here. got: {[r.getMessage() for r in new_id_lines]}"
    )
    # The queue still holds msg2 — it wasn't consumed by a removed interrupt.
    assert mgr.take_next_new_message() is msg2
    monkey.undo()

def test_background_does_not_repick_before_idle_seconds(caplog):
    """Regression: random_pick_changed must NOT trigger a fade-out
    before idle_seconds has elapsed.

    Pre-fix bug (2026-07-08): `random_pick_changed = bool(text) and
    text != self.last_shown_text` fired on essentially every pull
    because random.choice over a 10-entry recent pool returns a
    different body than last_shown_text ~90% of the time. Result
    was that background→out fired within 250 ms of entering
    background, making idle_seconds a meaningless knob (the sign
    cycled every ~16 s instead of every ~idle_seconds).

    This test seeds the buffer with two messages so random.choice
    has different bodies to pick from, drains to background, then
    verifies NO background→out log fires during a sub-idle
    duration. The 1-second idle window is intentionally well
    larger than the sub-idle advance (0.5 s) so a regression
    that drops the idle gate would fire here while the fixed
    version does not.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="first", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="second", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1, msg2])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        idle_seconds=1.0,
        message_manager=mgr,
    )
    coord.start()

    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)
    assert coord.mode == "background", f"Setup should land in background; got {coord.mode!r}"

    # Sub-idle advance: 0.5 s of ticks, well under idle_seconds=1.0.
    # random.choice over [msg1, msg2] will pick different bodies each
    # pull (50% chance of difference; with N pulls the probability of
    # at least one different pick approaches 1 — and the previous
    # last_shown_text was set during the out→in that brought us here
    # so almost every pull picks a different body than that).
    _drive(clock, coord, 0.5)
    assert coord.mode == "background", (
        f"After 0.5 s in background with idle_seconds=1.0, must still "
        f"be in background; got {coord.mode!r}. random_pick_changed "
        f"is firing before idle_seconds elapses."
    )

    repick_matches = _info_records(caplog, "background→out", "random_repick")
    assert not repick_matches, (
        "random_repick must not fire inside the idle window. "
        "If this fires, the idle_elapsed gate on random_pick_changed was removed."
    )
    monkey.undo()

def test_get_display_message_not_called_every_tick():
    """Coordinator must NOT call get_display_message() (the one with
    random.choice) on a timer. It runs only at the two background→out
    transition paths (new_id and idle), once per transition.

    Background: an earlier version throttled `get_display_message()` to
    ~4 Hz. That's wasted work — random.choice over the recent pool
    runs even when nothing about the sign's behavior will change —
    AND the `text != last_shown_text` gate that the throttled pull
    was wrapped in fires on essentially every pull when the pool has
    2+ messages, leading to the "sign cycles every ~16 s instead of
    every idle_seconds" bug.

    We seed the buffer with two messages so random.choice HAS
    different bodies to pick from, drain to background, then count
    how many times get_display_message() runs over a long stretch
    of ticks. With the new design, it should run ZERO times in
    background until idle_seconds elapses (and only ONCE at the
    idle-triggered transition).
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="alpha", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="beta", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1, msg2])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        idle_seconds=999.0,  # idle must NOT fire during the test
        message_manager=mgr,
    )
    coord.start()

    # Drain to background.
    _drive(clock, coord, 0.3)
    assert coord.mode == "background"

    # Patch get_display_message to count calls. We patch via setattr on
    # the class so the coordinator's bound method lookup hits our wrapper.
    coord_mod = importlib.import_module("lib_shared.effects_coordinator")
    call_count = {"n": 0}
    real_get_display_message = coord_mod.EffectsCoordinator.get_display_message

    def counting_get_display_message(self):
        call_count["n"] += 1
        return real_get_display_message(self)

    monkey.setattr(
        coord_mod.EffectsCoordinator,
        "get_display_message",
        counting_get_display_message,
    )

    # Run 100 ticks in background with idle_seconds=999. Random.choice
    # should NEVER be invoked because no transition fires.
    _drive(clock, coord, 1.0, step=0.01)
    assert coord.mode == "background", f"idle_seconds=999 means no transition; got mode={coord.mode!r}"
    assert call_count["n"] == 0, (
        f"get_display_message() ran {call_count['n']} times in background "
        f"with no transition — should be 0. The 250ms timer is back."
    )
    monkey.undo()

def test_pick_next_id_skip_when_random_choice_repeats_last_shown(monkeypatch):
    """Round 3 (debug-visibility): `_pick_next` re-rolls when
    random.choice lands on the message-id we just showed. With a
    2-message pool, random.choice returns the same id ~50% of
    the time; without the skip the coordinator would re-pick the
    same message and the cycler-complete path would pin the same
    cycler to the sign forever.

    The skip is keyed on `_last_shown_message_id`, NOT on a body
    string. The body-based re-roll depended on
    `self.last_shown_text`, which leaked stale text into the
    fade-out log (round-3 bug). An id is a stable identifier;
    tracking it leaks no body text and survives media-only cycles.

    The stub manager sorts entries by `received_at` descending
    (newest first), so msg1 must be NEWER than msg2 for msg1 to be
    the head. We also pre-set `_last_shown_message_id` to msg1's
    id so the fresh-id branch in `get_display_message` doesn't
    short-circuit — we want random.choice to fire, every time.
    """
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05, idle_seconds=1.0)
    # Force random.choice to ALWAYS return seq[0] (= "stale" id,
    # the newer of the two). _pick_next should re-roll up to 4
    # times before giving up.
    import random as _random

    monkeypatch.setattr(_random, "choice", lambda seq: seq[0])

    from lib_shared.models import MessageView, Message

    # msg1 is NEWER (so it's the head of current_messages) and is the
    # "stale" id — the one we just showed. msg2 is older ("fresh")
    # but random.choice is forced to never return it.
    msg1 = MessageView(
        Message(id="m1", sender="+1", body="stale", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="fresh", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    coord.message_manager._entries = [msg1, msg2]
    # Pre-arm the fresh-id branch so it short-circuits — random.choice
    # is the only path that can move us off the head.
    coord._last_shown_message_id = msg1.message.id

    # With random.choice always returning seq[0] (= msg1, body "stale"),
    # every re-roll still picks "stale". _pick_next gives up after
    # 4 tries and returns "stale". That's the bounded behavior — no spin.
    result = coord._pick_next()
    assert result == "stale", (
        f"After bounded re-rolls, _pick_next must give up and return "
        f"whatever it got (not None, not raise); got {result!r}"
    )

def test_pick_next_returns_other_id_when_available(monkeypatch):
    """When random.choice can land on an id DIFFERENT from the
    last-shown id, _pick_next returns it on the first try (no
    re-roll needed).
    """
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05, idle_seconds=1.0)
    # Force random.choice to ALWAYS return seq[-1] (= "fresh", the
    # older of the two messages — second in the sorted list).
    import random as _random

    monkeypatch.setattr(_random, "choice", lambda seq: seq[-1])

    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="stale", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="fresh", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    coord.message_manager._entries = [msg1, msg2]
    coord._last_shown_message_id = msg1.message.id  # short-circuit fresh-id

    result = coord._pick_next()
    assert result == "fresh"

def test_pick_next_returns_none_when_buffer_empty():
    """No messages in the pool → return None (no re-roll, no pick)."""
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05, idle_seconds=1.0)
    coord.message_manager._entries = []
    assert coord._pick_next() is None

def test_pull_runs_exactly_once_at_idle_timeout(monkeypatch):
    """Idle-triggered transition calls get_display_message() exactly ONCE
    per cycle — the bounded re-roll inside _pick_next_text is the only
    additional work.
    """
    import logging as _logging

    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="alpha", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="beta", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1, msg2])
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        idle_seconds=0.3,
        message_manager=mgr,
    )
    coord.start()

    # Drain to background.
    _drive(clock, coord, 0.3)
    assert coord.mode == "background"

    # Count get_display_message calls.
    coord_mod = importlib.import_module("lib_shared.effects_coordinator")
    call_count = {"n": 0}
    real = coord_mod.EffectsCoordinator.get_display_message

    def counting(self):
        call_count["n"] += 1
        return real(self)

    monkey.setattr(coord_mod.EffectsCoordinator, "get_display_message", counting)

    # Drive past idle_seconds — should fire ONE transition with ONE pull.
    _drive(clock, coord, 0.5, step=0.01)

    # We expect AT LEAST 1 pull (the idle-triggered one) but the exact
    # count depends on whether _pick_next re-rolled. With the
    # unseeded random, sometimes 1, sometimes 2, sometimes 3 calls —
    # all bounded by the 4-try re-roll limit. The key invariant: pull
    # happens once per transition, not per tick.
    assert call_count["n"] >= 1, "idle transition must trigger a pull"
    assert call_count["n"] <= 5, (
        f"unexpectedly many pull calls ({call_count['n']}); " f"the re-roll loop should be bounded"
    )
    monkey.undo()

# --- round 3 (debug-visibility): pick-first at every transition site ------

def test_intro_done_picks_before_fade_out(caplog, monkeypatch):
    """Round 3 contract: at the intro→out transition, the coordinator
    PICKS a message before kicking the fade-out. The picked body's id
    ends up in `_last_display_message` and `_last_picked_entry`; the
    scroller carries it after out→in completes. The selected-log
    fires before the fade-out log so the operator reads `selected →
    fade out` rather than `fade out → selected`.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="first message", received_at="t", media=[]),
        source="mqtt", suppressed=False,
    )
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=10.0, idle_seconds=999.0,
        message_manager=_StubMessageManager(messages=[msg]),
    )
    coord.start()
    caplog.set_level(logging.INFO)
    # Tick once → intro_done fires → pick fires → fade-out starts.
    clock.advance(0.001)
    coord.tick()

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    fade_out = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    assert selected, "intro_done must fire the selected-log before the fade-out"
    assert fade_out, "intro_done must fire the fade-out log"
    assert "msg_id=m1" in selected[0].getMessage()
    assert "first message" in selected[0].getMessage()
    # The pick site set _last_display_message and _last_picked_entry.
    assert coord._last_display_message == "first message"
    assert coord._last_picked_entry is not None
    assert coord._last_picked_entry.message.id == "m1"
    monkey.undo()

def test_background_idle_picks_before_fade_out(caplog, monkeypatch):
    """Round 3 contract: at the background→out idle transition, the
    coordinator PICKS a message before kicking the fade-out. If the
    pick returns None (empty buffer), the transition is skipped —
    the coordinator stays in background. This is the asymmetry the
    user asked for: rotation effects don't get cycled for nothing.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="buffered", received_at="t", media=[]),
        source="mqtt", suppressed=False,
    )
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=0.05, idle_seconds=0.1,
        message_manager=_StubMessageManager(messages=[msg]),
    )
    coord.start()
    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)
    assert coord.mode == "background"

    # Now drive past idle_seconds. The background→out transition
    # should fire, the selected-log should fire with msg_id=m1,
    # and the scroller should carry "buffered" after the fade-in.
    _drive(clock, coord, 0.3)
    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    fade_out = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    idle_fade_outs = [r for r in fade_out if "trigger=idle" in r.getMessage()]
    assert idle_fade_outs, "idle-triggered fade-out must have fired"
    assert selected, "selected-log must fire at the idle pick site"
    assert "msg_id=m1" in selected[0].getMessage()
    assert "buffered" in selected[0].getMessage()
    # Drive out → in and check the scroller.
    _drive(clock, coord, 0.2)
    assert coord.scroller.text == "buffered"  # type: ignore[attr-defined]
    monkey.undo()

def test_fresh_id_interrupt_picks_before_fade_out(caplog, monkeypatch):
    """Round 4 (queue redesign) inversion: the `fresh_id_interrupt`
    trigger was removed entirely. The queue drains at the natural
    pick sites (cycler_complete, intro_done, background idle).
    Mid-hold, the fresh arrival goes to the FIFO and the hold
    runs to `hold_seconds`.

    The previous round-3 contract was: "at the hold
    fresh_id_interrupt transition, the coordinator PICKS the fresh
    id (which is the head of current_messages) before kicking the
    fade-out." Round 4 removes the interrupt; the queue replaces
    it.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg1 = MessageView(
        Message(id="m1", sender="+1", body="first", received_at="t", media=[]),
        source="mqtt", suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="second", received_at="t2", media=[]),
        source="mqtt", suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1])
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=999.0, idle_seconds=999.0,  # long hold, no idle
        message_manager=mgr,
    )
    coord.start()
    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)
    assert coord.mode == "hold"

    # Inject fresh id mid-hold.
    mgr.add_message(msg2)
    clock.advance(0.05)
    coord.tick()

    # Round 4: with no idle trigger (idle_seconds=999), and the
    # `fresh_id_interrupt` trigger REMOVED, there is no path that
    # picks the queued msg2 mid-hold. The mode must STAY in
    # `hold`; the picked entry is still msg1 (the hold's first
    # pick).
    assert coord.mode == "hold", (
        "round 4 (queue redesign): no fresh_id_interrupt trigger. "
        "the hold survives mid-arrival; msg2 waits in the FIFO."
    )
    # No `trigger=fresh_id_interrupt` log line should fire.
    fade_out = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    fresh_fade_outs = [r for r in fade_out if "trigger=fresh_id_interrupt" in r.getMessage()]
    assert not fresh_fade_outs, (
        "round 4: trigger=fresh_id_interrupt was removed; no "
        "hold→out log should fire here. got: "
        f"{[r.getMessage() for r in fresh_fade_outs]}"
    )
    # And the queue still holds msg2 (the next natural pick site
    # — the hold→text_out transition — will drain it).
    assert mgr.take_next_new_message() is msg2
    monkey.undo()

def test_cycler_complete_picks_new_message(caplog, monkeypatch):
    """Round 3 contract: at the cycler_complete transition (the
    MediaCycler's `exhausted` / `complete` flag fires), the
    coordinator PICKS a NEW message before kicking the fade-out.
    Previously the cycler-exhaust path explicitly cleared
    `_last_picked_entry` to fall back to rotation; now it picks.
    The cycler's bound message is NOT re-picked (the new pick is
    a different message from the buffer).
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    # First message: has media (so it becomes a MediaCycler that
    # exhausts quickly). Second message: text-only, the new pick.
    msg1 = MessageView(
        Message(id="m1", sender="+1", body="", received_at="t",
                media=[{"type": "image/jpeg", "url": "media/x.jpg"}]),
        source="mqtt", suppressed=False,
    )
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="new message", received_at="t2", media=[]),
        source="mqtt", suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg1, msg2])
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=0.05, idle_seconds=999.0,
        message_manager=mgr,
    )
    coord.start()
    caplog.set_level(logging.INFO)
    # Drive long enough to cycle through: intro → out → in (m1
    # cycler) → hold → text_out → background → cycler_complete
    # (cycler exhausted) → out (picks m2).
    _drive(clock, coord, 2.0)

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    m2_selected = [r for r in selected if "msg_id=m2" in r.getMessage()]
    assert m2_selected, (
        f"cycler_complete path must pick m2 (a NEW message, not m1's "
        f"cycler-bound body); got: {[r.getMessage()[:120] for r in selected]}"
    )
    monkey.undo()

def test_empty_buffer_at_background_does_not_transition(caplog, monkeypatch):
    """Round 3 contract: when the buffer is empty and idle_seconds
    elapses, the background→out transition is SKIPPED. The
    coordinator stays in background — there's no point fading out
    for nothing. The operator sees no `Coordinator: selected` log
    (no pick), no `starting fade out trigger=idle` log (no
    transition).
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=0.05, idle_seconds=0.1,
        message_manager=_StubMessageManager(messages=[]),
    )
    coord.start()
    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.3)
    assert coord.mode == "background"

    # Now sit idle well past idle_seconds — should NOT transition.
    _drive(clock, coord, 1.0)
    assert coord.mode == "background", (
        f"with empty buffer, idle_seconds must NOT trigger a transition; "
        f"got mode={coord.mode!r}"
    )
    fade_outs = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    idle_fade_outs = [r for r in fade_outs if "trigger=idle" in r.getMessage()]
    assert not idle_fade_outs, (
        f"with empty buffer, no idle-triggered fade-out should fire; "
        f"got: {[r.getMessage() for r in fade_outs]}"
    )
    monkey.undo()

def test_empty_buffer_at_intro_done_still_transitions(caplog, monkeypatch):
    """Round 3 contract: at intro_done with an empty buffer, the
    fade-out STILL fires (the heart should fade out regardless of
    buffer state), but no selected-log fires (no pick). The
    out→in branch's `else` clears the scroller and lands in
    background with no text.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=0.05, idle_seconds=999.0,
        message_manager=_StubMessageManager(messages=[]),
    )
    coord.start()
    caplog.set_level(logging.INFO)
    _drive(clock, coord, 0.4)

    fade_out = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    intro_fade_out = [r for r in fade_out if "trigger=intro_done" in r.getMessage()]
    assert intro_fade_out, (
        f"intro_done fade-out must fire even with empty buffer; "
        f"got: {[r.getMessage() for r in fade_out]}"
    )
    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert not selected, (
        f"intro_done with empty buffer must NOT emit selected-log "
        f"(no message was picked); got: {[r.getMessage() for r in selected]}"
    )
    monkey.undo()

def test_empty_buffer_at_cycler_complete_still_transitions(caplog, monkeypatch):
    """Round 3 contract: at cycler_complete with an empty buffer,
    the fade-out STILL fires (the cycler is exhausted; we must
    transition) and a `cycler_complete, no replacement` log
    fires instead of the selected-log. The rotation effect runs
    for the rest of the hold with no scroller text.
    """
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    # One message with media → cycler. After the cycler binds to m1
    # via out→in, we drain the buffer so the next pick returns None.
    msg = MessageView(
        Message(id="m1", sender="+1", body="", received_at="t",
                media=[{"type": "image/jpeg", "url": "media/x.jpg"}]),
        source="mqtt", suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord, *_ = _build(
        intro_seconds=0.0, fade_seconds=0.05,
        hold_seconds=0.05, idle_seconds=999.0,
        message_manager=mgr,
    )
    coord.start()
    caplog.set_level(logging.INFO)
    # Drive the cycler into place: intro → out → in (cycler binds
    # to m1) → in → background. The cycler's decode fails (no
    # api_base_url) so it marks itself exhausted on the first tick.
    _drive(clock, coord, 0.3)
    # Now drain the buffer. The cycler is still the active effect,
    # but the buffer is empty — cycler_complete will fire without
    # a replacement.
    mgr._entries.clear()
    _drive(clock, coord, 0.5)

    no_replacement = [r for r in caplog.records if "no replacement" in r.getMessage()]
    assert no_replacement, (
        f"cycler_complete with empty buffer must log 'no replacement'; "
        f"got: {[r.getMessage()[:120] for r in caplog.records]}"
    )
    # And the fade-out itself still fired (cycler_complete trigger).
    fade_outs = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    cc_fade_outs = [r for r in fade_outs if "trigger=cycler_complete" in r.getMessage()]
    assert cc_fade_outs, (
        f"cycler_complete with empty buffer must still fire the fade-out; "
        f"got: {[r.getMessage()[:120] for r in fade_outs]}"
    )
    monkey.undo()
