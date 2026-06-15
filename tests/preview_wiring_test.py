"""Tests for the browser preview's live wiring of all SignConfig settings.

Covers:
- `build_effects(effect_settings, effect_classes)` — shared builder
- `EffectsCoordinator.apply_settings(...)` — live pacing + recent_count
- `ScrollerBase.set_color(...)` / `set_speed(...)` — live text updates
- `PreviewScroller` accepts the same kwargs as `ScrollerBase`
  (speed / color / frame_delay / offset_seconds)
- `preview_main.apply_config(cfg_dict)` — the live rebind path
"""

import importlib
import os
import sys
import time
from collections import deque
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

# Force the host's Pillow so PreviewScroller can import outside the
# browser. The browser's preview_main.py uses pyodide_js.loadPackage
# (top-level await) before importing the same modules, but in this
# test environment we go straight to imports.
_PILLOW_AVAILABLE = True
try:
    from PIL import Image  # noqa: F401
except ImportError:
    _PILLOW_AVAILABLE = False
    pytest.skip(
        "Pillow is required for preview_wiring_test on the host",
        allow_module_level=True,
    )

from lib_shared.effects_coordinator import EffectsCoordinator, build_effects
from lib_shared.models import EffectsSettings, SignConfig, TextSettings
from lib_shared.scroller_base import ScrollerBase

# --- stubs ------------------------------------------------------------------


class _StubCanvas:
    width = 64
    height = 64

    def clear(self):
        pass


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
    """A stub Effect class with the given class name (build_effects uses cls())."""

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


def _fx_factory():
    """A test factory that resolves a 3-name subset (A, B, C)."""
    A = _make_effect("A")
    B = _make_effect("B")
    C = _make_effect("C")
    table = {"A": A, "B": B, "C": C}

    def _factory(name):
        return table.get(name)

    return _factory


# --- build_effects ----------------------------------------------------------


def test_build_effects_returns_only_enabled():
    """build_effects skips entries with enabled=False."""
    settings = EffectsSettings(
        effects=[
            {"name": "A", "enabled": True},
            {"name": "B", "enabled": False},
            {"name": "C", "enabled": True},
        ]
    )
    out = build_effects(settings, _fx_factory())
    assert [type(x).__name__ for x in out] == ["A", "C"]


def test_build_effects_skips_unknown_names():
    """Unknown effect names are filtered (not raised) so the preview's
    factory can ingest a v2 payload that mentions PngDisplay/VideoDisplay."""
    settings = EffectsSettings(
        effects=[
            {"name": "A", "enabled": True},
            {"name": "PngDisplay", "enabled": True},  # not in test factory
            {"name": "B", "enabled": True},
        ]
    )
    out = build_effects(settings, _fx_factory())
    assert [type(x).__name__ for x in out] == ["A", "B"]


def test_build_effects_none_input_returns_empty():
    """build_effects(None) returns [] (the caller can supply a fallback)."""
    out = build_effects(None, _fx_factory())
    assert out == []


def test_build_effects_empty_list_returns_empty():
    """An effects list of all-disabled entries yields []."""
    settings = EffectsSettings(effects=[])
    out = build_effects(settings, _fx_factory())
    assert out == []


def test_build_effects_preserves_declaration_order():
    """Effects are returned in the order declared in the config."""
    settings = EffectsSettings(
        effects=[
            {"name": "C", "enabled": True},
            {"name": "A", "enabled": True},
            {"name": "B", "enabled": True},
        ]
    )
    out = build_effects(settings, _fx_factory())
    assert [type(x).__name__ for x in out] == ["C", "A", "B"]


# --- EffectsCoordinator.apply_settings --------------------------------------


def _build_coord(**kwargs):
    display = _StubDisplay()
    scroller = _StubScroller()
    fx = _make_effect("A")()
    heart = _make_effect("Heart")()
    return EffectsCoordinator(
        display=display,
        scroller=scroller,
        effects=[fx],
        heart=heart,
        **kwargs,
    )


def test_apply_settings_mutates_pacing():
    """apply_settings updates fade_seconds, hold_seconds, etc. in place."""
    coord = _build_coord(
        fade_seconds=2.0,
        hold_seconds=15.0,
        intro_seconds=5.0,
        idle_seconds=300.0,
    )
    new = EffectsSettings(fade_seconds=0.5, hold_seconds=7.0, intro_seconds=2.0, idle_seconds=120.0)
    coord.apply_settings(new)
    assert coord.fade_seconds == 0.5
    assert coord.hold_seconds == 7.0
    assert coord.intro_seconds == 2.0
    assert coord.idle_seconds == 120.0


def test_apply_settings_resizes_recent_deque():
    """apply_settings rebuilds the in-memory deque with the new maxlen."""
    coord = _build_coord(recent_count=5)
    # Seed a few entries (bypassing the dedup).
    for body in ("a", "b", "c", "d", "e", "f"):
        coord._recent.append(body)
    assert len(coord._recent) == 6  # maxlen was 5; deque trimmed on append

    coord.apply_settings(EffectsSettings(recent_count=3))
    assert coord._recent.maxlen == 3
    # The most recent 3 entries are retained.
    assert list(coord._recent)[-3:] == ["d", "e", "f"]


def test_apply_settings_preserves_recent_when_provider_set():
    """When a recent_provider is configured, the in-memory deque is
    never used — apply_settings does not touch it (no AttributeError)."""
    coord = _build_coord(recent_provider=lambda: [])
    coord.apply_settings(EffectsSettings(recent_count=99))
    # _recent is still the original deque, untouched.
    assert isinstance(coord._recent, deque)
    assert coord._recent.maxlen == coord.recent_count


def test_apply_settings_none_is_noop():
    """apply_settings(None) does nothing (defensive: the caller may
    pass None when the envelope is malformed)."""
    coord = _build_coord(fade_seconds=1.5)
    coord.apply_settings(None)
    assert coord.fade_seconds == 1.5


def test_apply_settings_does_not_touch_effects_rotation():
    """apply_settings is pacing-only — the effects list is left alone.
    The caller rebuilds the rotation via build_effects + .effects = ...,
    which is the documented pattern."""
    coord = _build_coord()
    fx_a = coord.effects[0]
    coord.apply_settings(EffectsSettings())
    assert coord.effects[0] is fx_a


# --- PreviewScroller: kwargs + live updates ---------------------------------


def _make_preview_scroller(**kwargs):
    """Build a PreviewScroller against the host's Pillow (no font file required)."""
    from heart_message_manager.preview_scroller import PreviewScroller

    # The fallback to Pillow's bundled default font doesn't need a path.
    return PreviewScroller(_StubDisplay(), **kwargs)


def test_preview_scroller_default_speed_is_3():
    """PreviewScroller with no kwargs uses the default speed (3)."""
    s = _make_preview_scroller()
    assert s.frame_delay == 0.040
    assert s.offset_seconds == 1.0


def test_preview_scroller_with_speed_5():
    """PreviewScroller(speed=5) translates to (0.020, 0.5)."""
    s = _make_preview_scroller(speed=5)
    assert s.frame_delay == 0.020
    assert s.offset_seconds == 0.5


def test_preview_scroller_with_speed_1():
    """PreviewScroller(speed=1) translates to (0.080, 1.5)."""
    s = _make_preview_scroller(speed=1)
    assert s.frame_delay == 0.080
    assert s.offset_seconds == 1.5


def test_preview_scroller_color_default_is_orange():
    """The preview's default color is the warm orange that matches the device."""
    s = _make_preview_scroller()
    assert s._color == 0xFF6400


def test_preview_scroller_color_kwarg():
    """PreviewScroller(color=...) sets _color."""
    s = _make_preview_scroller(color=0x123456)
    assert s._color == 0x123456


def test_preview_scroller_set_color_live():
    """set_color mutates _color in place (live config update)."""
    s = _make_preview_scroller()
    s.set_color(0xABCDEF)
    assert s._color == 0xABCDEF
    # color_tuple reflects the change.
    assert s.color_tuple() == (0xAB, 0xCD, 0xEF)


def test_preview_scroller_set_speed_live():
    """set_speed mutates frame_delay + offset_seconds (live config update)."""
    s = _make_preview_scroller()
    s.set_speed(5)
    assert s.frame_delay == 0.020
    assert s.offset_seconds == 0.5


def test_preview_scroller_set_speed_rejects_invalid():
    """set_speed raises ValueError on out-of-range / non-int / bool."""
    s = _make_preview_scroller()
    with pytest.raises(ValueError):
        s.set_speed(0)
    with pytest.raises(ValueError):
        s.set_speed(True)


# --- apply_config (preview_main.py live rebind) ----------------------------


def test_apply_config_rebinds_full_preview_state(tmp_path):
    """preview_main.apply_config takes a v2 dict and rewires everything
    end-to-end: builds a new effects rotation, applies pacing, updates
    the scroller's color + speed.

    We import preview_main dynamically and skip it on the host if its
    top-of-file pyodide_js import fails (the file is designed to run
    inside PyScript, not CPython).
    """
    try:
        pm = importlib.import_module("heart_message_manager.preview_main")
    except Exception as exc:
        pytest.skip(f"preview_main.py can't import under host CPython (expected): {exc}")

    # Seed the module-level coordinator + scroller (in case apply_config
    # relies on _coordinator already being initialized — it does).
    display = pm._display
    scroller = pm._scroller
    coord = pm._coordinator

    # Capture the existing rotation, color, speed so we can assert they change.
    original_speed = scroller.frame_delay, scroller.offset_seconds
    original_color = scroller._color

    # Build a v2 SignConfig dict with non-default settings.
    cfg = SignConfig(
        text_settings=TextSettings(speed=5, color=0x00FF00),
        effect_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
            fade_seconds=0.5,
            hold_seconds=10.0,
        ),
    )
    pm.apply_config(cfg.to_dict())

    # Scroller color + speed changed.
    assert scroller._color == 0x00FF00
    assert scroller.frame_delay == 0.020
    assert scroller.offset_seconds == 0.5
    # Coordinator pacing changed.
    assert coord.fade_seconds == 0.5
    assert coord.hold_seconds == 10.0
    # Effects rotation is non-empty (Fireworks was built).
    assert len(coord.effects) >= 1
    # Restore for cleanliness.
    scroller.set_color(original_color)
    scroller.set_speed(3)
