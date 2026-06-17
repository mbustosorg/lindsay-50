"""Tests for `EffectsCoordinator.apply_settings` (the on_change target).

The `on_change` callback on the MessageManager (constructed in
`heart-matrix-controller/main.py` and `heart-message-manager/preview_main.py`)
calls `coord.apply_settings(manager.config.effect_settings,
manager.config.text_settings)`. These tests pin down that contract:

1. `apply_settings(effect_settings, text_settings)` writes the pacing
   fields (`fade_seconds`, `hold_seconds`, `intro_seconds`,
   `idle_seconds`, `recent_count`).
2. `apply_settings` rebuilds `coordinator.effects` and resets
   `coordinator.idx` when the declared rotation changes.
3. `apply_settings` calls `coordinator.scroller.set_color(...)` and
   `coordinator.scroller.set_speed(...)` when `text_settings.color` or
   `text_settings.speed` changes.
4. `apply_settings` is idempotent on message-only emits: no effects
   rebuild, no scroller mutation (rotation + scroller hashes match).
5. The on_change closure in `heart-matrix-controller/main.py` and
   `heart-message-manager/preview_main.py` is a single function that
   calls `coord.apply_settings(manager.config.effect_settings,
   manager.config.text_settings)` (static check of the source files).
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import EffectsSettings, TextSettings


# --- shared stubs ------------------------------------------------------------


class _StubCanvas:
    width = 64
    height = 64


class _StubDisplay:
    def __init__(self):
        self.width = 64
        self.height = 64
        self.canvas = _StubCanvas()
        self.clear_called = 0

    def clear(self):
        self.clear_called += 1

    def render(self, effect, scroller):
        pass


class _StubScroller:
    def __init__(self):
        self.text = ""
        self._color = 0xFF6400
        self.frame_delay = 0.040
        self.offset_seconds = 1.0
        self.set_color_calls = []
        self.set_speed_calls = []

    def set_text(self, text, width):
        self.text = text

    def set_color(self, c):
        self.set_color_calls.append(c)
        self._color = c

    def set_speed(self, s):
        self.set_speed_calls.append(s)
        # Map speed (1..5) to (frame_delay, offset_seconds) like the
        # real PreviewScroller does.
        if s <= 1:
            self.frame_delay, self.offset_seconds = 0.080, 1.5
        elif s >= 5:
            self.frame_delay, self.offset_seconds = 0.020, 0.5
        else:
            self.frame_delay, self.offset_seconds = 0.040, 1.0

    def set_brightness(self, b):
        pass

    def tick(self, w):
        pass

    def render(self, canvas):
        pass


def _make_effect(name):

    class _Fx:
        def __init__(self):
            self.brightness = 1.0

        def tick(self):
            pass

        def render(self, canvas):
            pass

        def set_brightness(self, b):
            self.brightness = b

    _Fx.__name__ = name
    return _Fx


def _build(effect_settings=None, text_settings=None):
    mgr = SimpleNamespace(
        config=SimpleNamespace(
            effect_settings=effect_settings or EffectsSettings(),
            text_settings=text_settings or TextSettings(),
        )
    )
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=_StubDisplay(),
        scroller=_StubScroller(),
        effects=[_make_effect("Fireworks")()],
        heart=_make_effect("Heart")(),
    )
    return coord


# --- Scenario 1: apply_settings writes pacing fields -------------------------


def test_apply_settings_writes_pacing_fields():
    """apply_settings writes fade_seconds, hold_seconds, intro_seconds,
    idle_seconds, recent_count in place."""
    coord = _build()
    assert coord.fade_seconds == 2.0
    assert coord.hold_seconds == 15.0
    new = EffectsSettings(
        fade_seconds=0.5,
        hold_seconds=7.0,
        intro_seconds=2.0,
        idle_seconds=120.0,
        recent_count=3,
    )
    coord.apply_settings(new)
    assert coord.fade_seconds == 0.5
    assert coord.hold_seconds == 7.0
    assert coord.intro_seconds == 2.0
    assert coord.idle_seconds == 120.0
    assert coord.recent_count == 3


# --- Scenario 2: apply_settings rebuilds effects when rotation changes -------


def test_apply_settings_rebuilds_effects_on_rotation_change():
    """When the declared rotation changes, apply_settings rebuilds
    `coord.effects` and resets `coord.idx = -1`."""
    coord = _build()
    # First rotation: Fireworks only.
    rot1 = EffectsSettings(effects=[{"name": "Fireworks", "enabled": True}])
    coord.apply_settings(rot1)
    assert len(coord.effects) == 1
    assert type(coord.effects[0]).__name__ == "Fireworks"
    first_id = id(coord.effects[0])

    # Second rotation: Fireworks + Flame (a new one). The hash differs,
    # so the effects list is rebuilt.
    rot2 = EffectsSettings(
        effects=[
            {"name": "Fireworks", "enabled": True},
            {"name": "Flame", "enabled": True},
        ]
    )
    coord.apply_settings(rot2)
    assert len(coord.effects) == 2
    # The Fireworks instance is a fresh object (the rebuild constructs
    # new Effect instances even if the name matches).
    assert id(coord.effects[0]) != first_id
    # idx is reset to -1 so the next fade picks the head.
    assert coord.idx == -1


def test_apply_settings_keeps_effects_when_rotation_unchanged():
    """When the declared rotation is identical, apply_settings does
    NOT rebuild the effects list (idempotent on message-only emits)."""
    coord = _build()
    rot = EffectsSettings(effects=[{"name": "Fireworks", "enabled": True}])
    coord.apply_settings(rot)
    first_id = id(coord.effects[0])
    # Same rotation again.
    coord.apply_settings(rot)
    assert id(coord.effects[0]) == first_id


# --- Scenario 3: apply_settings mutates scroller on text settings change -----


def test_apply_settings_mutates_scroller_on_text_settings_change():
    """When text_settings.color or .speed changes, apply_settings calls
    set_color / set_speed on the scroller."""
    scroller = _StubScroller()
    coord = EffectsCoordinator(
        message_manager=SimpleNamespace(
            config=SimpleNamespace(
                effect_settings=EffectsSettings(),
                text_settings=TextSettings(),
            )
        ),
        display=_StubDisplay(),
        scroller=scroller,
        effects=[_make_effect("Fireworks")()],
        heart=_make_effect("Heart")(),
    )
    # Seed the scroller's current state via the first call (default
    # TextSettings). This establishes the baseline hash so the second
    # call can detect a real change.
    coord.apply_settings(EffectsSettings(), TextSettings())
    scroller.set_color_calls.clear()
    scroller.set_speed_calls.clear()

    # New color and speed.
    coord.apply_settings(EffectsSettings(), TextSettings(color=0x00FF00, speed=5))
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]
    assert scroller._color == 0x00FF00
    assert scroller.frame_delay == 0.020
    assert scroller.offset_seconds == 0.5


def test_apply_settings_skips_scroller_when_text_unchanged():
    """When text_settings is unchanged, apply_settings does NOT call
    set_color / set_speed (idempotent on message-only emits)."""
    scroller = _StubScroller()
    coord = EffectsCoordinator(
        message_manager=SimpleNamespace(
            config=SimpleNamespace(
                effect_settings=EffectsSettings(),
                text_settings=TextSettings(color=0x00FF00, speed=5),
            )
        ),
        display=_StubDisplay(),
        scroller=scroller,
        effects=[_make_effect("Fireworks")()],
        heart=_make_effect("Heart")(),
    )
    coord.apply_settings(EffectsSettings(), TextSettings(color=0x00FF00, speed=5))
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]
    # Second call with the same text settings: hashes match, no mutation.
    coord.apply_settings(EffectsSettings(), TextSettings(color=0x00FF00, speed=5))
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]


# --- Scenario 4: apply_settings is idempotent on message-only emits ----------


def test_apply_settings_is_idempotent_on_message_only_emit():
    """When only a new message arrives (no config change), apply_settings
    must NOT rebuild the rotation or mutate the scroller — the relevant
    fields are unchanged so the hashes match.

    The MessageManager's `_emit_change` fires for both `_handle_message`
    and `_handle_config`. A message-only emit should be a no-op for the
    coordinator's apply_settings work, even though the manager's
    internal state changed (the buffer grew).
    """
    scroller = _StubScroller()
    coord = EffectsCoordinator(
        message_manager=SimpleNamespace(
            config=SimpleNamespace(
                effect_settings=EffectsSettings(
                    effects=[{"name": "Fireworks", "enabled": True}],
                    fade_seconds=1.0,
                ),
                text_settings=TextSettings(color=0xFF6400, speed=3),
            )
        ),
        display=_StubDisplay(),
        scroller=scroller,
        effects=[_make_effect("Fireworks")()],
        heart=_make_effect("Heart")(),
    )
    # First call: seed the hashes. Effects rebuild (was empty), scroller
    # is updated (color/speed are the defaults, hashes match, so no
    # scroller mutation is expected on the first call).
    coord.apply_settings(
        EffectsSettings(effects=[{"name": "Fireworks", "enabled": True}], fade_seconds=1.0),
        TextSettings(color=0xFF6400, speed=3),
    )
    effects_id_before = id(coord.effects[0])
    scroller_color_calls_before = list(scroller.set_color_calls)
    scroller_speed_calls_before = list(scroller.set_speed_calls)

    # Second call: same effect_settings, same text_settings. Idempotent.
    coord.apply_settings(
        EffectsSettings(effects=[{"name": "Fireworks", "enabled": True}], fade_seconds=1.0),
        TextSettings(color=0xFF6400, speed=3),
    )
    assert id(coord.effects[0]) == effects_id_before
    assert scroller.set_color_calls == scroller_color_calls_before
    assert scroller.set_speed_calls == scroller_speed_calls_before


# --- Scenario 5: on_change closure in main.py / preview_main.py ---------------


def test_pi_main_uses_on_change_closure_with_apply_settings():
    """The Pi's `_on_change` closure calls
    `coord.apply_settings(manager.config.effect_settings,
    manager.config.text_settings)`."""
    p = Path(__file__).parent.parent.parent / "heart-matrix-controller" / "main.py"
    src = p.read_text(encoding="utf-8")
    # The closure must be passed as `on_change=_on_change` to MessageManager.
    assert re.search(r"on_change\s*=\s*_on_change", src), (
        "heart-matrix-controller/main.py must wire MessageManager(on_change=_on_change)"
    )
    # The closure body must call apply_settings with the two manager.config fields.
    assert "coordinator.apply_settings(manager.config.effect_settings, manager.config.text_settings)" in src, (
        "Pi _on_change must call coordinator.apply_settings(manager.config.effect_settings, manager.config.text_settings)"
    )


def test_app_main_uses_on_change_closure_with_apply_settings():
    """The browser's app-scoped `MessageManager.on_change` callback
    calls `coord.apply_settings(_message_manager.config.effect_settings,
    _message_manager.config.text_settings)` so the preview's pacing,
    rotation, and scroller color/speed reflect the change.

    The preview page does NOT construct a per-page MessageManager —
    the app-scoped one in `app_main.py` is the single source of truth.
    """
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "app_main.py"
    src = p.read_text(encoding="utf-8")
    # The manager's on_change callback must call apply_settings with
    # the two manager.config fields.
    assert "_coordinator.apply_settings(" in src, (
        "app_main.py must call _coordinator.apply_settings(...) in the on_change path"
    )
    assert "_message_manager.config.effect_settings" in src, (
        "app_main.py on_change must pass _message_manager.config.effect_settings"
    )
    assert "_message_manager.config.text_settings" in src, (
        "app_main.py on_change must pass _message_manager.config.text_settings"
    )
    # The coordinator must be constructed with the app-scoped manager.
    assert "EffectsCoordinator(message_manager=_message_manager)" in src, (
        "app_main.py must construct the app-scoped coordinator with message_manager=_message_manager"
    )


def test_preview_main_does_not_construct_per_page_manager():
    """The preview page is a thin render-layer shim — it must NOT
    create its own MessageManager (the app-scoped one in app_main.py
    is the single source of truth)."""
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "preview_main.py"
    src = p.read_text(encoding="utf-8")
    # preview_main.py must not import MessageManager.
    assert "from lib_shared.message_manager import" not in src, (
        "preview_main.py must not import MessageManager (the app-scoped one is the source of truth)"
    )
    # preview_main.py must not call the MessageManager constructor.
    assert "MessageManager(" not in src, (
        "preview_main.py must not construct a per-page MessageManager"
    )
    # preview_main.py must not reassign window._message_manager.
    assert "js.window._message_manager" not in src, (
        "preview_main.py must not reassign js.window._message_manager"
    )
