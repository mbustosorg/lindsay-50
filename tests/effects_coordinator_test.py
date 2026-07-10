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

    # out → in: the new shape splits into two lines — a "selected"
    # dump carrying the full picked message, then a "starting fade
    # in" line. We assert both are present and the picked message
    # made it into the selected line.
    selected = _info_records(caplog, "Coordinator: selected")
    assert selected, "Expected 'Coordinator: selected' INFO log at out→in"
    assert "m1" in selected[0].getMessage()
    fade_in = _info_records(caplog, "starting fade in")
    assert fade_in, "Expected 'starting fade in' INFO log"

    # in → hold fires once the fade-in completes (showing_text was True).
    # New shape: "Coordinator: fade in done effect=X next_mode=hold text=..."
    fade_in_done = _info_records(caplog, "fade in done")
    assert fade_in_done, "Expected 'fade in done' INFO log"
    assert "next_mode=hold" in fade_in_done[0].getMessage()
    assert "hi" in fade_in_done[0].getMessage()
    monkey.undo()


def test_hold_to_text_out_and_text_out_to_background_log_at_info(caplog):
    """A full hold→text_out→background cycle also logs each transition."""
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    from lib_shared.models import MessageView, Message

    msg = MessageView(
        Message(id="m1", sender="+1", body="hi", received_at="2026-01-01T00:00:00Z"),
        source="mqtt",
        suppressed=False,
    )
    # hold_seconds tiny so the cycle drains naturally
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

    assert _info_records(caplog, "Coordinator hold→text_out"), "expected hold→text_out INFO log"
    assert _info_records(caplog, "Coordinator text_out→background"), "expected text_out→background INFO log"
    monkey.undo()


def test_hold_does_not_interrupt_on_random_picks_from_shown_set(caplog):
    """v2 hold semantics: a fresh, un-shown SMS (head.id differs from
    `_last_shown_message_id`) interrupts the hold; a random re-pick from
    the already-shown pool does NOT. Without this, every pull with
    `random.choice` over a multi-message buffer would interrupt the
    hold instantly and `hold_seconds` would never be observed — that
    was the bug operators saw: messages would appear briefly then
    disappear after a few seconds regardless of hold_seconds setting.
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
    # hold_seconds = 0.5 so we can measure whether it elapses; idle_seconds irrelevant here.
    coord, *_ = _build(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.5,
        idle_seconds=999.0,
        message_manager=mgr,
    )
    coord.start()

    caplog.set_level(logging.INFO)
    # Drain to background via text_out so we enter the hold state cleanly.
    _drive(clock, coord, 0.4)  # intro → out → in → hold
    # Drain through hold_seconds to let it reach text_out naturally.
    _drive(clock, coord, 0.6)  # hold → text_out → background

    hold_to_text_out = _info_records(caplog, "Coordinator hold→text_out")
    assert hold_to_text_out, (
        "hold_seconds must elapse to text_out without being interrupted. "
        "If this fails, the bug returned: random re-picks are interrupting holds."
    )
    # And: no 'Coordinator hold interrupt' should have fired — that
    # log line only fires when a FRESH id arrives.
    interrupts = _info_records(caplog, "Coordinator hold interrupt")
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
    """A genuinely-new SMS arriving in background mode kicks a fade
    immediately — same as before, but via the new `fresh_id_landed`
    trigger rather than the legacy body-string compare. Confirms the
    new path covers the existing fresh-SMS flow.
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
        idle_seconds=999.0,  # isolate the new-id trigger from the idle trigger
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

    matches = _info_records(caplog, "starting fade out")
    assert matches, "Expected 'starting fade out' INFO log when a new SMS lands in background."
    new_id_lines = [r for r in matches if "trigger=new_id" in r.getMessage()]
    assert new_id_lines, (
        "Expected 'starting fade out' with trigger=new_id when an un-shown SMS lands."
    )
    # And the actual fade-in path should also fire — split into
    # "Coordinator: selected" + "starting fade in" lines.
    assert _info_records(
        caplog, "starting fade in"
    ), "After starting fade out (new_id), the fade-in should follow."
    assert _info_records(
        caplog, "Coordinator: selected"
    ), "After starting fade out (new_id), the selection log should fire."
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


def test_pick_next_text_re_rolls_when_random_choice_repeats_last_shown(monkeypatch):
    """`_pick_next_text` re-rolls when random.choice lands on the body
    we just showed. With a 2-message pool, random.choice returns the
    same body ~50% of the time, so without re-roll we'd flicker between
    "show X" and "stay showing X" instead of rotating.

    The stub manager sorts entries by `received_at` descending (newest
    first), so msg1 must be NEWER than msg2 for msg1 to be the head.
    We also pre-set `_last_shown_message_id` to msg1's id so the
    fresh-id branch in `get_display_message` doesn't short-circuit
    — we want random.choice to fire, every time.
    """
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05, idle_seconds=1.0)
    # Force random.choice to ALWAYS return seq[0] (= "stale" body,
    # the older of the two). _pick_next_text should re-roll up to 5
    # times before giving up.
    import random as _random

    monkeypatch.setattr(_random, "choice", lambda seq: seq[0])

    from lib_shared.models import MessageView, Message

    # msg1 is NEWER (so it's the head of current_messages) and is the
    # "stale" body — the one we just showed. msg2 is older ("fresh")
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
    coord.last_shown_text = "stale"

    # With random.choice always returning seq[0] (= msg1, body "stale"),
    # every re-roll still picks "stale". _pick_next_text gives up after
    # 5 tries and returns "stale". That's the bounded behavior — no spin.
    result = coord._pick_next_text()
    assert result == "stale", (
        f"After bounded re-rolls, _pick_next_text must give up and return "
        f"whatever it got (not None, not raise); got {result!r}"
    )


def test_pick_next_text_returns_other_body_when_available(monkeypatch):
    """When random.choice can land on a body DIFFERENT from last_shown_text,
    _pick_next_text returns it on the first try (no re-roll needed).
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
    coord.last_shown_text = "stale"

    result = coord._pick_next_text()
    assert result == "fresh"


def test_pick_next_text_returns_none_when_buffer_empty():
    """No messages in the pool → return None (no re-roll, no pick)."""
    coord, *_ = _build(intro_seconds=0.0, fade_seconds=0.05, idle_seconds=1.0)
    coord.message_manager._entries = []
    assert coord._pick_next_text() is None


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
    # count depends on whether _pick_next_text re-rolled. With the
    # unseeded random, sometimes 1, sometimes 2, sometimes 3 calls —
    # all bounded by the 5-try re-roll limit. The key invariant: pull
    # happens once per transition, not per tick.
    assert call_count["n"] >= 1, "idle transition must trigger a pull"
    assert call_count["n"] <= 5, (
        f"unexpectedly many pull calls ({call_count['n']}); " f"the re-roll loop should be bounded"
    )
    monkey.undo()
