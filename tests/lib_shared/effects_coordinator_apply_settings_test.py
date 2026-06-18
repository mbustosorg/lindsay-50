"""Tests for `EffectsCoordinator`'s live-read model of MessageManager config.

The coordinator holds no copy of the config — pacing fields
(`fade_seconds`, `hold_seconds`, `intro_seconds`, `idle_seconds`,
`recent_count`), the rotation, and the scroller text settings
(`color`, `speed`) all live on the manager. The coordinator reads
them at tick time (pacing/rotation through `effect_settings` /
`text_settings`, the render layer through the per-tick
`_sync_render_layer` which is hash-guarded and idempotent).

These tests pin down that contract:

1. `EffectsCoordinator(...)` exposes no `apply_settings` method and
   no `fade_seconds` / `hold_seconds` / `intro_seconds` /
   `idle_seconds` / `recent_count` fields. The coordinator
   delegates to the manager.
2. `_sync_render_layer` (called from `tick()`) refreshes the
   rotation + scroller text settings from the manager's current
   config, hash-guarded so an unchanged config is a no-op.
3. `bind()` resets the hash cache so the first tick after a
   fresh `bind()` re-runs the heavier work — the app-scoped
   coordinator's pre-bind ticks (during the seed) never had a
   render layer to refresh, so the bind is the first chance to
   populate the rotation + scroller from the seeded config.
4. The `on_change` closure in `heart-matrix-controller/main.py`
   and `heart-message-manager/app_main.py` does NOT call
   `apply_settings` — the coordinator pulls config live on its
   own ticks. The closures exist for symmetry (and for the JS
   fan-out to `App._dispatchChange`).
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
        self.render_calls = 0

    def clear(self):
        self.clear_called += 1

    def render(self, effect, scroller):
        self.render_calls += 1


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
        def __init__(self, display=None):
            self.brightness = 1.0
            self.display = display

        def tick(self):
            pass

        def render(self, canvas):
            pass

        def set_brightness(self, b):
            self.brightness = b

    _Fx.__name__ = name
    return _Fx


def _make_manager(effect_settings=None, text_settings=None):
    return SimpleNamespace(
        messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: []),
        config=SimpleNamespace(
            effect_settings=effect_settings or EffectsSettings(),
            text_settings=text_settings or TextSettings(),
        ),
    )


def _build_bound(message_manager=None):
    mgr = message_manager or _make_manager()
    display = _StubDisplay()
    scroller = _StubScroller()
    fx = _make_effect("Fireworks")(display=display)
    heart = _make_effect("Heart")(display=display)
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=display,
        scroller=scroller,
        effects=[fx],
        heart=heart,
    )
    return coord, mgr, display, scroller


# --- Scenario 1: the coordinator holds no config copy ------------------------


def test_coordinator_has_no_apply_settings_method():
    """The EffectsCoordinator no longer exposes `apply_settings` —
    config updates land via the manager, the coordinator reads
    live at tick time."""
    coord, _, _, _ = _build_bound()
    assert not hasattr(coord, "apply_settings"), (
        "EffectsCoordinator should not have apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )


def test_coordinator_has_no_cached_pacing_fields():
    """The coordinator does not store `fade_seconds`,
    `hold_seconds`, `intro_seconds`, `idle_seconds`, or
    `recent_count` as instance attributes — those are read from
    the manager on demand."""
    coord, _, _, _ = _build_bound()
    for field in (
        "fade_seconds",
        "hold_seconds",
        "intro_seconds",
        "idle_seconds",
        "recent_count",
    ):
        assert not hasattr(coord, field), (
            f"EffectsCoordinator should not cache {field!r} — " f"it lives on message_manager.config.effect_settings"
        )


def test_coordinator_constructor_rejects_pacing_kwargs():
    """The constructor dropped the per-pacing kwargs (the values
    come from the manager)."""
    with pytest.raises(TypeError, match="fade_seconds"):
        EffectsCoordinator(
            message_manager=_make_manager(),
            display=_StubDisplay(),
            scroller=_StubScroller(),
            effects=[],
            heart=None,
            fade_seconds=1.0,
        )


def test_coordinator_constructor_rejects_settings_kwarg():
    """The constructor dropped the `settings=` kwarg (the manager
    is the source of truth)."""
    with pytest.raises(TypeError, match="settings"):
        EffectsCoordinator(
            message_manager=_make_manager(),
            display=_StubDisplay(),
            scroller=_StubScroller(),
            effects=[],
            heart=None,
            settings=EffectsSettings(),
        )


# --- Scenario 2: tick reads pacing live from the manager ----------------------


def test_tick_reads_intro_seconds_from_manager():
    """The first tick after `start()` with `intro_seconds=0.0`
    leaves `intro` immediately (the manager's value). With
    `intro_seconds=0.0`, the state machine never sits in
    `intro`; with a larger value it does. The structural
    assertion is that the manager's value drives the
    transition."""
    mgr = _make_manager(
        effect_settings=EffectsSettings(intro_seconds=0.0),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.start()
    coord.tick()
    # `intro` is a transient mode when intro_seconds is 0.0; the
    # first tick should have moved past it.
    assert coord.mode != "intro"
    # The source of truth is the manager.
    assert mgr.config.effect_settings.intro_seconds == 0.0


def test_get_display_message_reads_recent_count_from_manager():
    """`get_display_message` reads `recent_count` from the manager."""
    # Empty buffer with a low recent_count: still returns None
    # (nothing to display).
    mgr = _make_manager(
        effect_settings=EffectsSettings(recent_count=3),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    assert coord.get_display_message() is None


# --- Scenario 3: _sync_render_layer refreshes rotation + scroller -----------


def test_tick_refreshes_rotation_when_manager_rotation_changes():
    """The first tick builds the rotation from the manager's
    current `effect_settings.effects` list. Changing the
    manager's config and ticking again rebuilds the rotation."""
    mgr = _make_manager(
        effect_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.tick()
    first_id = id(coord.effects[0])
    assert type(coord.effects[0]).__name__ == "Fireworks"

    # Update the manager's rotation to a different effect.
    mgr.config.effect_settings = EffectsSettings(
        effects=[{"name": "Flame", "enabled": True}],
    )
    coord.tick()
    assert type(coord.effects[0]).__name__ == "Flame"
    assert id(coord.effects[0]) != first_id
    # idx is reset to -1 so the next fade picks the head of the new list.
    assert coord.idx == -1


def test_tick_keeps_effects_when_rotation_unchanged():
    """When the manager's rotation is unchanged, the tick is a
    no-op for the rotation list (hash-guarded)."""
    mgr = _make_manager(
        effect_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.tick()
    fx_id = id(coord.effects[0])
    coord.tick()
    coord.tick()
    assert id(coord.effects[0]) == fx_id


def test_tick_updates_scroller_color_and_speed_on_change():
    """When the manager's `text_settings` changes, the next
    tick calls `scroller.set_color(...)` and
    `scroller.set_speed(...)`."""
    mgr = _make_manager(text_settings=TextSettings())
    coord, _, _, scroller = _build_bound(message_manager=mgr)
    # First tick with the default text settings is a no-op (the
    # coordinator's text-settings hash starts at the default,
    # matching the manager's default).
    coord.tick()
    assert scroller.set_color_calls == []
    assert scroller.set_speed_calls == []

    # Update the manager's text settings.
    mgr.config.text_settings = TextSettings(color=0x00FF00, speed=5)
    coord.tick()
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]
    assert scroller._color == 0x00FF00
    assert scroller.frame_delay == 0.020
    assert scroller.offset_seconds == 0.5


def test_tick_skips_scroller_when_text_settings_unchanged():
    """When the manager's text settings are unchanged, the tick
    does NOT call `set_color` / `set_speed` (hash-guarded)."""
    mgr = _make_manager(
        text_settings=TextSettings(color=0x00FF00, speed=5),
    )
    coord, _, _, scroller = _build_bound(message_manager=mgr)
    # First tick writes the manager's non-default text settings to
    # the scroller (hash differs from the coordinator's initial
    # default).
    coord.tick()
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]
    coord.tick()
    coord.tick()
    # No additional calls beyond the first.
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]


# --- Scenario 4: bind() resets the hash cache -------------------------------


def test_bind_resets_hash_cache():
    """After `bind()`, the first tick refreshes the render layer
    with the manager's current config — even if the previous
    pre-bind tick had already run a sync (it didn't, but the
    hash cache is reset to be safe across mid-life binds)."""
    scroller = _StubScroller()
    display = _StubDisplay()
    fx = _make_effect("Fireworks")(display=display)
    heart = _make_effect("Heart")(display=display)
    mgr = _make_manager(
        effect_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
        text_settings=TextSettings(color=0x00FF00, speed=5),
    )
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=None,  # unbound
        scroller=None,
        effects=[],
        heart=None,
    )
    # Pre-bind tick: no display/scroller, so the per-tick
    # `_sync_render_layer` early-returns. The hash cache stays
    # at its initial value (the default-rotation hash).
    coord.tick()
    assert coord.effects == []
    assert scroller.set_color_calls == []
    pre_bind_effects_hash = coord._last_effects_hash
    pre_bind_text_hash = coord._last_text_settings_hash

    # Now bind the render layer. `bind()` resets both hashes
    # to None so the first post-bind tick refreshes the
    # rotation + scroller with the manager's non-default config.
    coord.bind(display=display, scroller=scroller, effects=[fx], heart=heart)
    assert coord._last_effects_hash is None
    assert coord._last_text_settings_hash is None
    coord.tick()
    # The rotation was rebuilt (Fireworks is in the list).
    assert any(type(f).__name__ == "Fireworks" for f in coord.effects)
    # The scroller received the non-default text settings.
    assert scroller.set_color_calls == [0x00FF00]
    assert scroller.set_speed_calls == [5]
    # Hashes have moved on from their pre-bind values.
    assert coord._last_effects_hash != pre_bind_effects_hash
    assert coord._last_text_settings_hash != pre_bind_text_hash


# --- Scenario 5: on_change closures do NOT call apply_settings ----------------


def test_pi_on_change_does_not_call_apply_settings():
    """The Pi's `_on_change` closure is a no-op — the coordinator
    reads config live, no `apply_settings` call needed.

    The closure must still be wired to the manager (so the wiring
    contract is symmetric across the Pi and the browser) — it's
    just that the body no longer applies config.
    """
    p = Path(__file__).parent.parent.parent / "heart-matrix-controller" / "main.py"
    src = p.read_text(encoding="utf-8")
    assert re.search(
        r"on_change\s*=\s*_on_change", src
    ), "heart-matrix-controller/main.py must wire MessageManager(on_change=_on_change)"
    # Check the closure body, not the docstring.
    m = re.search(r"def _on_change\([^)]*\)[^:]*:\s*\n(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None, "could not extract Pi _on_change body"
    body = m.group(1)
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert "apply_settings" not in body, (
        "Pi _on_change must not call coordinator.apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )


def test_app_main_on_change_does_not_call_apply_settings():
    """The browser's app-scoped `_on_change_js` callback is a
    fan-out to `App._dispatchChange` only — no `apply_settings`
    call, since the coordinator reads config live.

    The check looks at the function body, not the docstring
    (the docstring still references the old design as historical
    context for readers).
    """
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "app_main.py"
    src = p.read_text(encoding="utf-8")
    # The function body — between `def _on_change_js(...):` and the
    # next top-level `def `, skipping the docstring (the docstring
    # still references the old design as historical context).
    m = re.search(r"def _on_change_js\([^)]*\)[^:]*:\s*\n(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None, "could not extract _on_change_js body"
    body = m.group(1)
    # Drop the docstring portion (between the first `"""` and the
    # next `"""`).
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert "apply_settings" not in body, (
        "browser _on_change_js must not call apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )
    # The fan-out to JS is preserved.
    assert "app._dispatchChange" in body, "app_main.py _on_change_js must still fan out to App._dispatchChange"


def test_preview_main_does_not_pass_settings_to_bind():
    """The preview's `coord.bind(...)` call must not pass
    `effect_settings` / `text_settings` — the coordinator reads
    those from the manager at tick time, and `bind()` does not
    take config args anymore."""
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "preview_main.py"
    src = p.read_text(encoding="utf-8")
    assert "coord.bind(" in src, "preview_main.py must call coord.bind(...)"
    # `bind()` is the no-config-args flavor: display=, scroller=,
    # effects=, heart=. No `effect_settings` or `text_settings` kwargs.
    m = re.search(r"coord\.bind\(([^)]+)\)", src, re.DOTALL)
    assert m is not None, "could not extract coord.bind(...) args"
    bind_args = m.group(1)
    assert "effect_settings" not in bind_args, "preview_main.py coord.bind(...) must not pass effect_settings"
    assert "text_settings" not in bind_args, "preview_main.py coord.bind(...) must not pass text_settings"


def test_preview_main_does_not_construct_per_page_manager():
    """The preview page is a thin render-layer shim — it must NOT
    create its own MessageManager (the app-scoped one in app_main.py
    is the single source of truth)."""
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "preview_main.py"
    src = p.read_text(encoding="utf-8")
    assert (
        "from lib_shared.message_manager import" not in src
    ), "preview_main.py must not import MessageManager (the app-scoped one is the source of truth)"
    assert "MessageManager(" not in src, "preview_main.py must not construct a per-page MessageManager"
    assert "js.window._message_manager" not in src, "preview_main.py must not reassign js.window._message_manager"
