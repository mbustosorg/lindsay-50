"""Tests for lib_shared.effects_coordinator.EffectsCoordinator.

Covers the lifecycle state machine: intro → out → in → hold → text_out →
background; the brightness-ramp endpoints; the pending_text consume-on-
transition behavior; the request_message deque dedup; and the display.render
call per tick.
"""

import importlib
import sys
import time
from pathlib import Path

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

    def set_text(self, text, width):
        self.set_text_calls.append((text, width))
        self.text = text

    def set_brightness(self, b):
        self.set_brightness_calls.append(b)
        self._brightness = b

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
    recent_provider=None,
):
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    heart = _make_effect("Heart")()
    coord = importlib.import_module("lib_shared.effects_coordinator").EffectsCoordinator(
        display=display,
        scroller=scroller,
        effects=[fx_a, fx_b],
        heart=heart,
        recent_provider=recent_provider,
        fade_seconds=fade_seconds,
        hold_seconds=hold_seconds,
        intro_seconds=intro_seconds,
        idle_seconds=idle_seconds,
    )
    return coord, display, scroller, fx_a, fx_b, heart


def _drive(clock, coord, seconds, step=0.01):
    """Advance clock by `seconds` in `step` increments, calling tick() each step."""
    elapsed = 0.0
    while elapsed < seconds:
        clock.advance(step)
        coord.tick()
        elapsed += step


# --- state-machine tests ----------------------------------------------------


def test_intro_then_out_then_in_then_background():
    """Mode progresses intro → out → in → background when no text is queued."""
    clock = _Clock()
    importlib.import_module("lib_shared.effects_coordinator")  # ensure module is in
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start(None)

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
    # No text was queued, so we land in background
    assert coord.mode == "background"
    assert coord.current is fx_a
    monkey.undo()


def test_idx_advances_on_fade_out_complete():
    """After one full out cycle, idx has advanced by 1 modulo len(effects)."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start(None)
    _drive(clock, coord, 0.1)  # out + in
    assert coord.idx == 0
    # Trigger another out: send a message and let it complete
    coord.request_message("hi")
    _drive(clock, coord, 0.1)  # out + in
    assert coord.idx == 1
    assert coord.current is fx_b
    monkey.undo()


def test_pending_text_consumed_on_out_to_in():
    """request_message(text) sets pending_text; the next out → in consumes it."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start(None)
    _drive(clock, coord, 0.05)  # intro → out
    coord.request_message("hello")
    assert coord.pending_text == "hello"
    _drive(clock, coord, 0.1)  # out completes; consumes pending_text
    assert coord.pending_text is None
    assert scroller.text == "hello"
    assert coord.last_shown_text == "hello"
    monkey.undo()


def test_hold_mode_interrupted_by_new_message():
    """A new message during hold immediately kicks a fade-out, no waiting for hold_seconds."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05, hold_seconds=999.0)
    coord.request_message("first")  # queue a message so we reach hold
    coord.start(None)
    _drive(clock, coord, 0.2)  # intro → out → in → hold
    assert coord.mode == "hold"
    coord.request_message("second")
    clock.advance(0.001)
    coord.tick()
    # Immediately transitioned to out without waiting for hold_seconds
    assert coord.mode == "out"
    assert coord.pending_text == "second"
    monkey.undo()


def test_request_message_empty_is_noop():
    """Empty / None body doesn't kick a fade or alter pending_text."""
    coord, *_ = _build()
    coord.request_message("")
    coord.request_message(None)
    assert coord.pending_text is None
    assert coord.mode == "intro"


def test_request_message_dedupes_internal_deque():
    """Two consecutive request_message calls with the same body store it once in _recent."""
    coord, *_ = _build()
    coord.request_message("hello")
    coord.request_message("hello")
    assert list(coord._recent).count("hello") == 1


def test_brightness_ramp_endpoints():
    """Out completes with set_brightness(0.0); in completes with set_brightness(1.0)."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start(None)
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
    coord.start(None)
    for _ in range(3):
        clock.advance(0.01)
        coord.tick()
    assert len(display.render_calls) == 3
    for effect, scr in display.render_calls:
        assert scr is scroller


def test_recent_provider_used_when_set():
    """When recent_provider is given, _random_recent reads from it (not the deque)."""
    fake_entry = type("E", (), {"message": type("M", (), {"body": "from-provider"})()})()
    coord, *_ = _build(recent_provider=lambda: [fake_entry])
    coord.request_message("from-deque")
    body = coord._random_recent()
    # recent_provider wins
    assert body == "from-provider"


def test_internal_deque_used_when_no_recent_provider():
    """When recent_provider is None, _random_recent reads from the internal deque."""
    coord, *_ = _build()
    coord.request_message("queued")
    body = coord._random_recent()
    assert body == "queued"


def test_current_effect_name_and_text():
    """current_effect_name / current_text mirror the active effect + scroller text."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    coord, display, scroller, fx_a, fx_b, heart = _build(intro_seconds=0.0, fade_seconds=0.05)
    coord.start(None)
    # At start, current is the heart
    assert coord.current_effect_name == "Heart"
    # After driving through to in, current is fx_a
    _drive(clock, coord, 0.1)
    assert coord.current_effect_name == "A"
    # current_text reflects the scroller's text (or '' when nothing is shown)
    assert coord.current_text == ""
    monkey.undo()
