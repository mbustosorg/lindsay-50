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
    """

    def __init__(self, messages=None, effects_settings=None, text_settings=None):
        from lib_shared.models import EffectsSettings, TextSettings

        self.messages = SimpleNamespace(get_messages=self._get_messages)
        self._entries = list(messages or [])
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
        self._entries.append(view)


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
    """After a full out cycle triggered by a pulled message, idx advances by 1 modulo len(effects)."""
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
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05, message_manager=mgr)
    coord.start()
    _drive(clock, coord, 0.3)  # intro → out → in → hold (m1 shown)
    assert coord.idx == 0
    # First message has been shown; the hold state now waits for a fresh pull.
    # Add a NEW message and let the throttled pull pick it up; the coordinator's
    # hold→out transition will advance idx.
    msg2 = MessageView(
        Message(id="m2", sender="+1", body="next", received_at="2026-01-02T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    mgr.add_message(msg2)
    # Bump past the PULL_INTERVAL so the next tick pulls the fresh message.
    clock.advance(0.5)
    coord.tick()
    assert coord.mode == "out"
    _drive(clock, coord, 0.2)  # out + in
    assert coord.idx == 1
    assert coord.current is fx_b
    monkey.undo()


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
    assert coord.last_shown_text == "hello"
    monkey.undo()


def test_hold_mode_interrupted_by_new_message():
    """A new (different-id) message during hold kicks a fade-out, no waiting for hold_seconds."""
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
    # Immediately transitioned to out without waiting for hold_seconds
    assert coord.mode == "out"
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
