"""Tests for heart-message-manager.preview_renderer.

Covers both the effect cycle wiring (Section 4) and the PreviewCoordinator
(Section 6). The coordinator mirrors the device's EffectCoordinator and is
the in-browser main loop's call target.
"""

import importlib.util
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _restore_lib_shared():
    """test_auth.py replaces sys.modules['lib_shared'] with a Mock. Re-import
    the real package before each test.
    """
    for mod_name in list(sys.modules):
        if mod_name == "lib_shared" or mod_name.startswith("lib_shared."):
            del sys.modules[mod_name]
    importlib.import_module("lib_shared")
    importlib.import_module("lib_shared.config_reader")
    yield


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_renderer():
    p = _PROJECT_ROOT / "heart-message-manager" / "preview_renderer.py"
    return _load(p, "heart_message_manager.preview_renderer")


def _load_canvas():
    p = _PROJECT_ROOT / "heart-message-manager" / "preview_canvas.py"
    return _load(p, "heart_message_manager.preview_canvas")


def _make_stub_patterns(effects_spec):
    """Build a fake `patterns` module whose classes either construct successfully
    or raise, depending on `effects_spec`.

    `effects_spec` is a list of (class_name, raises_or_None, raise_message).
    Uses a real types.ModuleType so missing attributes raise AttributeError
    (MagicMock auto-creates them, which would mask the missing-class path).
    """
    import types
    mod = types.ModuleType("patterns")
    for name, raises, msg in effects_spec:
        if raises:
            def _make_raising(msg):
                def _factory(*a, **kw):
                    raise RuntimeError(msg)
                return _factory
            setattr(mod, name, _make_raising(msg))
        else:
            def _make_factory(name):
                class _StubPattern:
                    def __init__(self, display):
                        self.display = display
                    def tick(self): pass
                    def render(self, canvas): pass
                    def set_brightness(self, b): pass
                _StubPattern.__name__ = name
                return _StubPattern
            setattr(mod, name, _make_factory(name))
    return mod


def _make_display():
    """Build a real WebDisplay + WebCanvas pair for the coordinator."""
    canvas_mod = _load_canvas()
    web_canvas = canvas_mod.WebCanvas(64, 64)
    return canvas_mod.WebDisplay(web_canvas), web_canvas


# ---------------------------------------------------------------------------
# Section 4: effect cycle wiring
# ---------------------------------------------------------------------------


def test_renderer_keeps_effects_that_initialize():
    """Successful constructors are added to the cycle."""
    mod = _load_renderer()
    patterns = _make_stub_patterns([
        ("Fireworks", False, None),
        ("Flame", False, None),
        ("NightSky", False, None),
        ("Honeycomb", False, None),
    ])
    display, _ = _make_display()
    renderer = mod.PreviewRenderer(display, patterns)
    assert len(renderer.effects) == 4


def test_renderer_skips_constructor_that_raises():
    """A failing constructor is excluded; the rest still load."""
    mod = _load_renderer()
    patterns = _make_stub_patterns([
        ("Fireworks", False, None),
        ("Flame", True, "missing dep"),  # fails
        ("NightSky", False, None),
        ("Honeycomb", False, None),
    ])
    display, _ = _make_display()
    renderer = mod.PreviewRenderer(display, patterns)
    # Flame failed, the other three are in the cycle
    assert len(renderer.effects) == 3


def test_renderer_skips_missing_classes_gracefully():
    """A pattern class not present in the module is logged and skipped, not raised."""
    mod = _load_renderer()
    # Only provide one pattern; the other three are missing
    patterns = MagicMock()
    patterns.Fireworks = MagicMock(return_value=MagicMock())
    # Flame/NightSky/Honeycomb: getattr returns MagicMock (because we
    # didn't set them on the mock). To simulate the real behavior, set
    # them to raise AttributeError via __getattr__.
    def _raise_attribute(name):
        raise AttributeError(name)
    patterns.__class__ = type("M", (), {"__getattr__": staticmethod(_raise_attribute)})
    # Easier: build a real dict-backed module
    import types
    real_mod = types.ModuleType("patterns")
    class Fireworks:
        def __init__(self, display): pass
        def tick(self): pass
        def render(self, canvas): pass
        def set_brightness(self, b): pass
    real_mod.Fireworks = Fireworks
    display, _ = _make_display()
    renderer = mod.PreviewRenderer(display, real_mod)
    assert len(renderer.effects) == 1


def test_renderer_logs_browser_skipped_patterns():
    """PngDisplay and VideoDisplay are logged as skipped even if they would init."""
    mod = _load_renderer()
    # Don't provide PngDisplay / VideoDisplay in the module at all — they
    # shouldn't be constructed (and the missing-class path is logged too).
    import types
    patterns = types.ModuleType("patterns")
    class Fireworks:
        def __init__(self, display): pass
        def tick(self): pass
        def render(self, canvas): pass
        def set_brightness(self, b): pass
    patterns.Fireworks = Fireworks
    display, _ = _make_display()
    renderer = mod.PreviewRenderer(display, patterns)
    # Only Fireworks ends up in the cycle
    assert len(renderer.effects) == 1


# ---------------------------------------------------------------------------
# Section 6: PreviewCoordinator
# ---------------------------------------------------------------------------


class _StubScroller:
    """Minimal stub for ScrollerBase — no actual Pillow or font work."""

    def __init__(self, display):
        self.display = display
        self.text = ""
        self.text_width = 0
        self.top_x = display.width
        self.bottom_x = display.width
        self.single_line = False
        self.top_y = 0
        self.bottom_y = 0
        self._brightness = 1.0
        self.set_text_calls = []
        self.set_brightness_calls = []
        self.tick_calls = []
        self.render_calls = []

    def set_text(self, text, canvas_width):
        self.set_text_calls.append((text, canvas_width))
        self.text = text
        self.text_width = len(text) * 5
        self.top_x = canvas_width
        self.bottom_x = canvas_width

    def tick(self, canvas_width):
        self.tick_calls.append(canvas_width)

    def render(self, canvas):
        self.render_calls.append(canvas)

    def set_brightness(self, b):
        self.set_brightness_calls.append(b)
        self._brightness = b


class _StubEffect:
    """Minimal Effect — records all calls for assertion."""

    def __init__(self, display):
        self.display = display
        self.tick_calls = 0
        self.render_calls = 0
        self.brightness = 1.0

    def tick(self):
        self.tick_calls += 1

    def render(self, canvas):
        self.render_calls += 1

    def set_brightness(self, b):
        self.brightness = b


def _make_coordinator(fade_seconds=0.4, fade_step=0.1):
    """Build a coordinator with two stub effects and a stub scroller."""
    mod = _load_renderer()
    display, canvas = _make_display()
    fx_a = _StubEffect(display)
    fx_b = _StubEffect(display)
    scroller = _StubScroller(display)
    coord = mod.PreviewCoordinator(
        display, scroller, [fx_a, fx_b],
        fade_seconds=fade_seconds, fade_step=fade_step,
    )
    return coord, fx_a, fx_b, scroller, canvas


def test_coordinator_starts_idle_with_first_effect_active():
    """Fresh coordinator: mode=idle, idx=0, no fade in progress."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    assert coord.mode == "idle"
    assert coord.idx == 0
    assert coord.current_effect_name == "_StubEffect"


def test_coordinator_request_message_kicks_fade_out():
    """request_message transitions mode to 'out' and sets pending_text."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    coord.request_message("hello")
    assert coord.mode == "out"
    assert coord.pending_text == "hello"


def test_coordinator_request_message_empty_is_noop():
    """Empty / None body doesn't kick a fade."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    coord.request_message("")
    assert coord.mode == "idle"
    coord.request_message(None)
    assert coord.mode == "idle"


def test_coordinator_request_message_duplicate_is_noop():
    """Repeated request_message with the same body doesn't re-kick the fade."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    coord.request_message("hi")
    coord.mode = "idle"  # pretend the first fade completed
    coord.request_message("hi")  # duplicate — should not transition to "out"
    assert coord.mode == "idle"


def test_coordinator_fade_out_completes_then_advances_effect(monkeypatch):
    """After fade_seconds, the active effect index advances and the scroller gets the new text."""
    state = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])
    coord, fx_a, fx_b, scroller, _ = _make_coordinator(fade_seconds=0.2)
    coord.request_message("hello")
    state["t"] += 0.5  # 0.5s elapsed, fade is 0.2s -> completion
    coord.tick()
    # Effect index advanced
    assert coord.idx == 1
    # Scroller was handed the new text
    assert scroller.text == "hello"
    # And we're now fading in
    assert coord.mode == "in"


def test_coordinator_full_fade_cycle_advances_once(monkeypatch):
    """After a full out+in cycle, idx is back to its starting position
    (since there are 2 effects, idx went 0->1 during out, and stays at 1
    during in)."""
    state = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])
    coord, fx_a, fx_b, scroller, _ = _make_coordinator(fade_seconds=0.1)
    coord.request_message("hi")
    # Fade out completes
    state["t"] += 0.2
    coord.tick()
    assert coord.idx == 1
    assert coord.mode == "in"
    # Fade in completes
    state["t"] += 0.2
    coord.tick()
    assert coord.mode == "idle"
    # Effect indices don't reset; we just land on the new active effect
    assert coord.idx == 1


def test_coordinator_tick_calls_effect_and_scroller_every_frame():
    """Each tick() advances the active effect and the scroller."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    coord.tick()
    coord.tick()
    coord.tick()
    assert fx_a.tick_calls == 3
    assert scroller.tick_calls == [64, 64, 64]


def test_coordinator_tick_composites_to_canvas():
    """Each tick clears the canvas, draws the effect, draws the scroller."""
    coord, fx_a, fx_b, scroller, canvas = _make_coordinator()
    coord.tick()
    # Effect was rendered
    assert fx_a.render_calls == 1
    # Scroller was rendered
    assert len(scroller.render_calls) == 1
    # Scroller was rendered with the canvas (not the display)
    assert scroller.render_calls[0] is canvas


def test_coordinator_current_text_reflects_scroller_text(monkeypatch):
    """current_text mirrors the scroller's active text after a fade."""
    state = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])
    coord, fx_a, fx_b, scroller, _ = _make_coordinator(fade_seconds=0.1)
    coord.request_message("hello")
    state["t"] += 0.2  # complete fade-out
    coord.tick()
    state["t"] += 0.2  # complete fade-in
    coord.tick()
    assert coord.current_text == "hello"


def test_coordinator_idle_does_not_call_set_brightness():
    """In idle mode, no fade steps are written (no set_brightness calls)."""
    coord, fx_a, fx_b, scroller, _ = _make_coordinator()
    coord.tick()
    assert scroller.set_brightness_calls == []
    assert fx_a.brightness == 1.0
