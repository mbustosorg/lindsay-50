"""Tests for lib_shared.display_base.DisplayBase."""

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_display_base():
    return _load(
        "lib_shared.display_base",
        _PROJECT_ROOT / "lib_shared" / "display_base.py",
    )


# Note: no autouse `_restore_lib_shared` fixture. test_auth.py's
# `app` fixture already restores the real lib_shared submodules on
# teardown, so a sibling autouse wipe is redundant (and would force
# re-imports of `lib_shared.message_manager` and friends, breaking
# downstream tests that captured a reference to those modules at
# import time).


def test_display_base_cannot_be_instantiated_directly():
    """DisplayBase is abstract — calling render()/clear() on a bare instance raises."""
    mod = _load_display_base()
    d = mod.DisplayBase()
    with pytest.raises(NotImplementedError):
        d.render(None, None)
    with pytest.raises(NotImplementedError):
        d.clear()


def test_subclass_render_called_once_per_tick():
    """A DisplayBase subclass's render is called exactly once per coordinator tick."""
    from lib_shared.effects_coordinator import EffectsCoordinator

    mod = _load_display_base()

    class _StubDisplay(mod.DisplayBase):
        def __init__(self):
            self.width = 8
            self.height = 8
            self.canvas = object()
            self.render_calls = []

        def clear(self):
            pass

        def render(self, effect, scroller):
            self.render_calls.append((effect, scroller))

    class _StubEffect:
        def __init__(self):
            self.tick_calls = 0
            self.brightness = 1.0

        def tick(self):
            self.tick_calls += 1

        def render(self, canvas):
            pass

        def set_brightness(self, b):
            self.brightness = b

    class _StubScroller:
        text = ""

        def set_text(self, text, w):
            self.text = text

        def set_brightness(self, b):
            pass

        def tick(self, w):
            pass

        def render(self, canvas):
            pass

    display = _StubDisplay()
    effect = _StubEffect()
    scroller = _StubScroller()
    heart = _StubEffect()

    coord = EffectsCoordinator(
        display=display,
        scroller=scroller,
        effects=[effect],
        heart=heart,
        intro_seconds=0,
        fade_seconds=0.01,
    )
    coord.start(None)
    coord.tick()
    # First tick: intro → out, current is still heart (idx hasn't advanced yet)
    assert len(display.render_calls) == 1
    assert display.render_calls[0] == (heart, scroller)
    coord.tick()
    # After enough ticks, idx advances to the first effect
    # Drive until idx advances
    while coord.idx < 0:
        coord.tick()
    # Each call passes (current_effect, scroller)
    for fx, scr in display.render_calls:
        assert scr is scroller
        assert fx in (heart, effect)
