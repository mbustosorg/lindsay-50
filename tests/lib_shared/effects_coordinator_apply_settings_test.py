"""Tests for `EffectsCoordinator`'s settings-refresh model.

The coordinator holds no copy of the config — pacing fields
(`fade_seconds`, `hold_seconds`, `intro_seconds`,
`lookback_days`), the `selector_algorithm` field, the rotation, and
the scroller text settings (`color`, `speed`) all live on the
manager. The coordinator reads pacing fields at tick time via the
`effects_settings` / `text_settings` properties (which delegate to
the manager's `get_effects_settings` / `get_text_settings`).

The rotation rebuild and scroller color / speed application only
happen at cycle boundaries — specifically, at the `out→in`
transition via `_refresh_render_layer_from_settings`. Tick itself
is a pure render: it advances the state machine and draws the
current frame, but never rebuilds effects or calls scroller
setters. This is the architectural split the user pinned:

  "tick should just update the panel with what's currently
   rendering. we should just refresh settings on the next cycle."

These tests pin down that contract:

1. `EffectsCoordinator(...)` exposes no `apply_settings` method and
   no `fade_seconds` / `hold_seconds` / `intro_seconds` /
   `idle_seconds` / `lookback_days` / `selector_algorithm`
   fields. The coordinator delegates to the manager.
2. `tick()` does NOT rebuild the rotation or call scroller
   `set_color` / `set_speed`. The render layer stays as-is
   across many ticks in the same mode.
3. The `out→in` cycle transition DOES rebuild the rotation and
   apply the current scroller color / speed from the manager —
   the `_refresh_render_layer_from_settings` helper handles
   both. This is the "settings refresh on the next cycle" half.
4. `bind()` does not need to reset any per-tick diff sentinels
   (the old `_last_rotation` / `_last_text_color` / `_last_text_speed`
   fields are gone — there are no per-tick diffs to gate).
5. The `on_change` closure in `heart-matrix-controller/main.py`
   and `heart-message-manager/app_main.py` does NOT call
   `apply_settings` — the coordinator reads config live on its
   own ticks.
"""

import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from lib_shared.effects_coordinator import EffectsCoordinator
from lib_shared.models import EffectsSettings, TextSettings, Message, MessageView

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


def _make_manager(effects_settings=None, text_settings=None, messages=None):
    msgs = list(messages or [])
    # Default `lookback_days=MAX` so hardcoded 2026-XX-XX
    # `received_at` values used throughout this file stay
    # eligible. Tests that target the eligibility filter pass
    # `effects_settings=EffectsSettings(lookback_days=...)`.
    effective_es = effects_settings or EffectsSettings(
        lookback_days=EffectsSettings.MAX_LOOKBACK_DAYS,
        selector_algorithm="weighted",
    )
    mgr = SimpleNamespace(
        messages=SimpleNamespace(get_messages=lambda limit=100, suppress=True: list(msgs[:limit])),
        config=SimpleNamespace(
            effects_settings=effective_es,
            text_settings=text_settings or TextSettings(),
        ),
    )
    mgr.get_messages = lambda limit=100, suppress=True: list(msgs[:limit])
    mgr.get_effects_settings = lambda: mgr.config.effects_settings
    mgr.get_text_settings = lambda: mgr.config.text_settings
    return mgr


def _make_view(message_id: str, body: str, received_at: str) -> MessageView:
    return MessageView(
        Message(id=message_id, sender="+1", body=body, received_at=received_at),
        source="mqtt",
        suppressed=False,
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
    `hold_seconds`, `intro_seconds`, `lookback_days`, or
    `selector_algorithm` as instance attributes — those are read
    from the manager on demand. (`idle_seconds` was promoted to
    a module-level constant `IDLE_SECONDS_AFTER_HOLD` per the
    user's behavioral-knobs-in-code rule; the selection
    algorithm is resolved freshly via `make_selector(...)` on
    every pick.)"""
    coord, _, _, _ = _build_bound()
    for field in ("fade_seconds", "hold_seconds", "intro_seconds", "lookback_days", "selector_algorithm"):
        assert not hasattr(coord, field), (
            f"EffectsCoordinator should not cache {field!r} — " f"it lives on message_manager.config.effects_settings"
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


def test_coordinator_has_no_per_tick_diff_sentinels():
    """The per-tick diff sentinels are gone. There is no
    `_last_rotation`, `_last_text_color`, or `_last_text_speed`
    field — tick doesn't gate any work against these. Settings
    refresh happens only at cycle boundaries."""
    coord, _, _, _ = _build_bound()
    for field in ("_last_rotation", "_last_text_color", "_last_text_speed"):
        assert not hasattr(coord, field), (
            f"EffectsCoordinator should not have {field!r} — "
            f"tick should just render, settings refresh on the next cycle."
        )


# --- Scenario 2: tick reads pacing live, but does NOT apply settings --------


def test_tick_reads_intro_seconds_from_manager():
    """The first tick after `start()` with `intro_seconds=0.0`
    leaves `intro` immediately (the manager's value). With
    `intro_seconds=0.0`, the state machine never sits in
    `intro`; with a larger value it does. The structural
    assertion is that the manager's value drives the
    transition."""
    mgr = _make_manager(
        effects_settings=EffectsSettings(intro_seconds=0.0),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.start()
    coord.tick()
    assert coord.mode != "intro"
    assert mgr.config.effects_settings.intro_seconds == 0.0


def test_get_display_message_reads_lookback_days_from_manager():
    """`get_display_message` reads `lookback_days` from the manager."""
    mgr = _make_manager(
        effects_settings=EffectsSettings(lookback_days=3),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    assert coord.get_display_message() is None


def test_tick_does_not_rebuild_rotation_when_rotation_changes():
    """Tick is a pure render — it does NOT rebuild the rotation
    even when the manager's rotation config changes. The change
    surfaces at the NEXT `out→in` cycle boundary via
    `_refresh_render_layer_from_settings`."""
    mgr = _make_manager(
        effects_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.start()
    coord.tick()
    first_id = id(coord.effects[0])
    assert type(coord.effects[0]).__name__ == "Fireworks"

    # Update the manager's rotation to a different effect.
    mgr.config.effects_settings = EffectsSettings(
        effects=[{"name": "NightSky", "enabled": True}],
    )
    # Tick many times in `intro` (the only mode that doesn't
    # apply settings) — the rotation MUST stay as Fireworks
    # until a cycle boundary fires.
    for _ in range(20):
        coord.tick()
    assert type(coord.effects[0]).__name__ == "Fireworks"
    assert id(coord.effects[0]) == first_id


def test_tick_does_not_call_scroller_setters():
    """Tick never calls `scroller.set_color(...)` or
    `scroller.set_speed(...)`. The setters fire ONLY at the
    cycle boundary (out→in) via
    `_refresh_render_layer_from_settings`. This is the
    architectural split: tick = pure render, settings apply on
    the next cycle."""
    mgr = _make_manager(text_settings=TextSettings())
    coord, _, _, scroller = _build_bound(message_manager=mgr)
    coord.start()
    coord.tick()
    assert scroller.set_color_calls == []
    assert scroller.set_speed_calls == []

    # Change the manager's text settings and tick repeatedly.
    # The setters should still NOT be called — we're still in
    # tick-land, not at a cycle boundary.
    mgr.config.text_settings = TextSettings(color=0x00FF00, speed=5)
    for _ in range(20):
        coord.tick()
    assert scroller.set_color_calls == []
    assert scroller.set_speed_calls == []


# --- Scenario 3: cycle boundary (out→in) DOES apply settings ----------------


def test_out_to_in_rebuilds_rotation_from_manager():
    """At the `out→in` transition, `_refresh_render_layer_from_settings`
    rebuilds the effects list from the manager's current rotation.
    This is the "settings refresh on the next cycle" half of the
    architectural split — operator changes to the rotation land
    at the next message-fade-in, not on the next tick."""
    clock = [1000.0]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("lib_shared.effects_coordinator.IDLE_SECONDS_AFTER_HOLD", 0.05)
    msg = _make_view("m1", "hi", "2026-01-01T00:00:00Z")
    mgr = _make_manager(
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.05,
            hold_seconds=0.05,
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
        messages=[msg],
    )
    coord, _, _, _ = _build_bound(message_manager=mgr)
    coord.start()
    coord.tick()

    # Update the manager's rotation before the cycle completes.
    mgr.config.effects_settings = EffectsSettings(
        intro_seconds=0.0,
        fade_seconds=0.05,
        hold_seconds=0.05,
        effects=[{"name": "NightSky", "enabled": True}],
    )

    # Drive JUST past the FIRST out→in transition (which is at
    # ~0.10s after intro→out at t=0.01). 15 ticks × 0.01s = 0.15s.
    # After that we're in 'in' or 'hold' — the setter has fired
    # exactly once.
    for _ in range(15):
        clock[0] += 0.01
        coord.tick()

    # The first out→in has fired — the rotation was rebuilt.
    assert any(type(f).__name__ == "NightSky" for f in coord.effects), (
        "After the first out→in, the rotation must be rebuilt from "
        "the manager's current config. The cycle-boundary refresh is "
        "broken if the rotation is still the pre-cycle one."
    )
    # After the rebuild + the +1 idx advance at out→in, idx wraps to 0
    # (we always start from effects[0] on a fresh rotation).
    assert coord.idx == 0, "out→in rebuild resets idx to -1, then the +1 advance wraps to 0"
    monkeypatch.undo()


def test_out_to_in_applies_scroller_color_and_speed():
    """At the `out→in` transition, `_refresh_render_layer_from_settings`
    applies the manager's current text_settings to the scroller.
    Color and speed changes land at the next cycle, not per tick."""
    clock = [1000.0]
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr("lib_shared.effects_coordinator.IDLE_SECONDS_AFTER_HOLD", 0.05)
    msg = _make_view("m1", "hi", "2026-01-01T00:00:00Z")
    mgr = _make_manager(
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.05,
            hold_seconds=0.05,
        ),
        text_settings=TextSettings(color=0x00FF00, speed=5),
        messages=[msg],
    )
    coord, _, _, scroller = _build_bound(message_manager=mgr)
    coord.start()
    # Tick in intro — setters NOT called.
    coord.tick()
    assert scroller.set_color_calls == []
    assert scroller.set_speed_calls == []

    # Drive JUST past the FIRST out→in transition. With
    # IDLE_SECONDS_AFTER_HOLD=0.05 and fade_seconds=0.05, the
    # first out→in lands at t≈0.10s after start. 15 ticks at
    # 0.01s each = 0.15s — well past the first out→in, not yet
    # at the second one.
    for _ in range(15):
        clock[0] += 0.01
        coord.tick()

    # The first out→in has fired — the setters should have been
    # called exactly once with the manager's text settings.
    assert scroller.set_color_calls == [0x00FF00], (
        f"scroller.set_color should fire ONCE at the out→in transition; " f"got {scroller.set_color_calls}"
    )
    assert scroller.set_speed_calls == [5], (
        f"scroller.set_speed should fire ONCE at the out→in transition; " f"got {scroller.set_speed_calls}"
    )
    assert scroller._color == 0x00FF00
    monkeypatch.undo()


# --- Scenario 4: bind() doesn't need to reset any diff sentinels -------------


def test_bind_does_not_set_per_tick_diff_sentinels():
    """The old `bind()` reset `_last_rotation`, `_last_text_color`,
    and `_last_text_speed` to None so the first post-bind tick
    would refresh the render layer. With the new cycle-boundary
    model, bind() doesn't need to do that — the render layer
    refreshes on the next out→in transition, period."""
    scroller = _StubScroller()
    display = _StubDisplay()
    fx = _make_effect("Fireworks")(display=display)
    heart = _make_effect("Heart")(display=display)
    mgr = _make_manager(
        effects_settings=EffectsSettings(
            effects=[{"name": "Fireworks", "enabled": True}],
        ),
        text_settings=TextSettings(color=0x00FF00, speed=5),
    )
    coord = EffectsCoordinator(
        message_manager=mgr,
        display=None,
        scroller=None,
        effects=[],
        heart=None,
    )
    coord.tick()
    assert coord.effects == []
    assert scroller.set_color_calls == []

    coord.bind(display=display, scroller=scroller, effects=[fx], heart=heart)
    # No `_last_*` diff sentinels exist — bind() doesn't set them.
    assert not hasattr(coord, "_last_rotation")
    assert not hasattr(coord, "_last_text_color")
    assert not hasattr(coord, "_last_text_speed")


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
    m = re.search(r"def _on_change\([^)]*\)[^:]*:\s*\n(.*?)(?=\ndef |\Z)", src, re.DOTALL)
    assert m is not None, "could not extract Pi _on_change body"
    body = m.group(1)
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert "apply_settings" not in body, (
        "Pi _on_change must not call coordinator.apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )


def test_app_main_on_change_does_not_call_apply_settings():
    """The browser's `_on_change_js` callback is a fan-out to
    `App._dispatchChange` only — no `apply_settings` call, since
    the coordinator reads config live.

    History: pre-#48 this looked at `app_main.py:_on_change_js`.
    The 2026-07-23 round-5 simplification moved the on_change
    callback to `dashboard_runtime._on_change_js` (no per-
    generation discriminator — the runtime is built ONCE per
    page load; refresh to restart). The contract is unchanged.
    """
    p = (
        Path(__file__).parent.parent.parent
        / "heart-message-manager"
        / "dashboard_runtime.py"
    )
    src = p.read_text(encoding="utf-8")
    # Look for the closure body inside `_on_change_js`. The 2026-07-23
    # round-5 simplification moved the callback into a nested closure
    # inside `install_runtime()`; the regex matches the function
    # declaration (with or without a return-type annotation) and any
    # subsequent `def ` at the same or parent indent.
    m = re.search(
        r"def _on_change_js\(\)[^:]*:\s*\n((?:[ \t]+.*\n|\s*\n)*?)(?=^[ \t]*def |^def |\Z)",
        src,
        re.MULTILINE,
    )
    assert m is not None, "could not extract _on_change_js body"
    body = m.group(1)
    body = re.sub(r'"""[\s\S]*?"""', "", body, count=1)
    assert "apply_settings" not in body, (
        "browser on_change callback must not call apply_settings — "
        "the coordinator reads config live from message_manager.config"
    )
    assert "_dispatchChange" in body, (
        "browser on_change must still fan out to App._dispatchChange"
    )


def test_preview_main_does_not_pass_settings_to_bind():
    """The preview's `coord.bind(...)` call must not pass
    `effects_settings` / `text_settings` — the coordinator reads
    those from the manager at tick time, and `bind()` does not
    take config args anymore."""
    p = Path(__file__).parent.parent.parent / "heart-message-manager" / "preview_main.py"
    src = p.read_text(encoding="utf-8")
    assert "coord.bind(" in src, "preview_main.py must call coord.bind(...)"
    m = re.search(r"coord\.bind\(([^)]+)\)", src, re.DOTALL)
    assert m is not None, "could not extract coord.bind(...) args"
    bind_args = m.group(1)
    assert "effects_settings" not in bind_args, "preview_main.py coord.bind(...) must not pass effects_settings"
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
