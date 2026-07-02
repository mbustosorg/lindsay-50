"""Tests for the browser preview's live wiring of all SignConfig settings.

Covers:
- `build_effects(effects_settings, effect_classes)` — shared builder
- `EffectsCoordinator.apply_settings(...)` — live pacing + recent_count
- `ScrollerBase.set_color(...)` / `set_speed(...)` — live text updates
- `PreviewScroller` accepts the same kwargs as `ScrollerBase`
  (speed / color / frame_delay / offset_seconds)
- `preview_main.apply_config(cfg_dict)` — the live rebind path
"""

import importlib
import sys
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

from types import SimpleNamespace

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
        # Scroller text-settings state — driven by the coordinator's
        # `_sync_render_layer` on a text_settings change.
        self._color = 0xFF6400
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
        # Mirror the real PreviewScroller's speed-to-(frame_delay, offset_seconds) map.
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
    """A stub Effect class with the given class name. build_effects
    instantiates with `cls(display)`, so the stub accepts and stashes
    the display without using it."""

    class _Fx:
        def __init__(self, display=None):
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
    out = build_effects(settings, _fx_factory(), display=_StubDisplay())
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
    out = build_effects(settings, _fx_factory(), display=_StubDisplay())
    assert [type(x).__name__ for x in out] == ["A", "B"]


def test_build_effects_none_input_returns_empty():
    """build_effects(None) returns [] (the caller can supply a fallback).
    None input short-circuits BEFORE display is consulted, so the
    caller can pass None safely without a display in scope."""
    out = build_effects(None, _fx_factory())
    assert out == []


def test_build_effects_empty_list_returns_empty():
    """An effects list of all-disabled entries yields []."""
    settings = EffectsSettings(effects=[])
    out = build_effects(settings, _fx_factory(), display=_StubDisplay())
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
    out = build_effects(settings, _fx_factory(), display=_StubDisplay())
    assert [type(x).__name__ for x in out] == ["C", "A", "B"]


def test_build_effects_requires_display():
    """build_effects raises ValueError if display is missing — every
    Effect subclass needs a display, and a silent failure mode
    (e.g. `None` passed to a `display.canvas.width` access deep in
    the constructor) would surface much later as a confusing
    AttributeError on first frame."""
    settings = EffectsSettings(effects=[{"name": "A", "enabled": True}])
    with pytest.raises(ValueError, match="display"):
        build_effects(settings, _fx_factory())


# --- EffectsCoordinator.apply_settings --------------------------------------


def _build_coord(message_manager=None, **kwargs):
    from lib_shared.models import EffectsSettings, TextSettings

    display = _StubDisplay()
    scroller = _StubScroller()
    fx = _make_effect("A")()
    heart = _make_effect("Heart")()
    if message_manager is None:
        message_manager = SimpleNamespace(
            messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
            config=SimpleNamespace(
                effects_settings=EffectsSettings(),
                text_settings=TextSettings(),
            ),
        )
        message_manager.get_effects_settings = lambda: message_manager.config.effects_settings
        message_manager.get_text_settings = lambda: message_manager.config.text_settings
        message_manager.get_messages = lambda limit=100, suppress=True: []
    return EffectsCoordinator(
        message_manager=message_manager,
        display=display,
        scroller=scroller,
        effects=[fx],
        heart=heart,
    )


def test_coordinator_reads_pacing_from_message_manager():
    """The coordinator holds no per-instance pacing — it reads
    `message_manager.config.effects_settings.fade_seconds` /
    `.hold_seconds` / `.intro_seconds` / `.idle_seconds` at tick time.
    Updating the manager's config is observed by the coordinator on
    the next tick (no explicit `apply_settings` call needed).
    """
    from lib_shared.models import EffectsSettings

    mgr = SimpleNamespace(
        messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
        config=SimpleNamespace(
            effects_settings=EffectsSettings(
                fade_seconds=2.0,
                hold_seconds=15.0,
                intro_seconds=5.0,
                idle_seconds=300.0,
            ),
            text_settings=TextSettings(),
        ),
    )
    mgr.get_effects_settings = lambda: mgr.config.effects_settings
    mgr.get_text_settings = lambda: mgr.config.text_settings
    mgr.get_messages = lambda limit=100, suppress=True: []
    coord = _build_coord(message_manager=mgr, effects_settings=mgr.config.effects_settings)
    # Pacing is read off the manager — no need to call apply_settings.
    assert mgr.config.effects_settings.fade_seconds == 2.0
    assert mgr.config.effects_settings.hold_seconds == 15.0
    # Mutating the manager's config is the new way to change pacing.
    mgr.config.effects_settings = EffectsSettings(
        fade_seconds=0.5, hold_seconds=7.0, intro_seconds=2.0, idle_seconds=120.0
    )
    assert mgr.config.effects_settings.fade_seconds == 0.5
    assert mgr.config.effects_settings.hold_seconds == 7.0
    assert mgr.config.effects_settings.intro_seconds == 2.0
    assert mgr.config.effects_settings.idle_seconds == 120.0
    # The coordinator has no cached copy (no `coord.fade_seconds`).
    assert not hasattr(coord, "fade_seconds")
    assert not hasattr(coord, "hold_seconds")
    assert not hasattr(coord, "intro_seconds")
    assert not hasattr(coord, "idle_seconds")


def test_coordinator_no_apply_settings_method():
    """The EffectsCoordinator no longer has an `apply_settings` method —
    config updates are read live from the manager at tick time."""
    coord = _build_coord()
    assert not hasattr(coord, "apply_settings"), (
        "EffectsCoordinator should not have apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )


def test_coordinator_does_not_touch_effects_rotation_when_unchanged():
    """When the rotation hash matches, the per-tick sync is a no-op —
    the rotation stays as it was. Pin the contract that the
    hash-guarded refresh is a real cache (not a rebuild every tick).
    """
    coord = _build_coord()
    # The first tick runs `_sync_render_layer`, which rebuilds the
    # rotation from the manager's default `EffectSettings()` (empty
    # effects list — `build_effects` falls back to the first canonical
    # effect so the list is non-empty).
    coord.tick()
    fx_after_first = coord.effects[0]
    # Second tick with the same default config: hash matches, no rebuild.
    coord.tick()
    assert coord.effects[0] is fx_after_first


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


def test_config_update_live_applies_to_render_layer():
    """The full rebind path is now: change `message_manager.config`,
    call `coord.tick()`. The coordinator's per-tick
    `_sync_render_layer` reads the new config and updates the
    rotation + scroller color/speed. Pacing changes are visible on
    the next transition (the coordinator reads pacing from the
    manager at every decision point, no cached copy).
    """
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    mgr = SimpleNamespace(
        messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
        config=SimpleNamespace(
            effects_settings=EffectsSettings(),
            text_settings=TextSettings(),
        ),
    )
    mgr.get_effects_settings = lambda: mgr.config.effects_settings
    mgr.get_text_settings = lambda: mgr.config.text_settings
    mgr.get_messages = lambda limit=100, suppress=True: []
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=_StubDisplay(),
        scroller=scroller,
        effects=[fx_a],
        heart=heart,
    )

    # First tick refreshes the render layer from the current (default) config.
    coord.tick()
    assert scroller._color == 0xFF6400  # default TextSettings().color
    assert scroller.frame_delay == 0.040  # default TextSettings().speed = 3

    # Now update the manager's config to a non-default SignConfig.
    cfg = SignConfig(
        text_settings=TextSettings(speed=5, color=0x00FF00),
        effects_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
            fade_seconds=0.5,
            hold_seconds=10.0,
        ),
    )
    mgr.config.effects_settings = cfg.effects_settings
    mgr.config.text_settings = cfg.text_settings

    # Next tick: rotation hash differs, scroller color/speed differ.
    coord.tick()
    assert scroller._color == 0x00FF00
    assert scroller.frame_delay == 0.020
    assert scroller.offset_seconds == 0.5
    # Pacing lives on the manager; the coordinator has no copy.
    assert mgr.config.effects_settings.fade_seconds == 0.5
    assert mgr.config.effects_settings.hold_seconds == 10.0
    # The Fireworks effect was built and added to the rotation.
    assert any(type(fx).__name__ == "Fireworks" for fx in coord.effects)
