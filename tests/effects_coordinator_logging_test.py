"""Tests for the operator-visibility logging changes.

Two related fixes from the user's debug-visibility bug report:

1. **Selection-time INFO log.** When the EffectsCoordinator picks a
   message (out→in transition), it now emits a single line carrying
   the FULL picked message (id, sender, body, media count) plus the
   chosen effect. Operators tailing the Pi's journal can grep
   "Coordinator: selected" to reconstruct what's on the sign
   without cross-referencing the rotation rebuild + `set_text` +
   `out→in` chain.

2. **Fade-log consolidation.** The verbose multi-arg INFO logs at
   `_begin_out` / `out→in` / `in→background` are replaced with
   single-line forms — `Coordinator: starting fade out|in|done` —
   keeping the same info but cutting per-cycle noise from ~6 lines
   to 2 (selection + fade-in start, plus the fade-out start).

These tests pin the new log shapes via `caplog`. They don't drive
the full state machine (that's covered by `effects_coordinator_test.py`); they
just construct a coordinator, drive `tick()` to a single
transition, and assert what got logged.
"""

from __future__ import annotations

import importlib
import logging
import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT / "tests"))

from lib_shared.models import (  # noqa: E402
    EffectsSettings,
    Message,
    MessageView,
    TextSettings,
)
from effects_coordinator_test import _StubMessageManager  # noqa: E402


# --- helpers ----------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _make_effect(name):
    class _Eff:
        def __init__(self):
            self.name = name
            self.brightness = 1.0

        def tick(self):
            pass

        def set_brightness(self, b):
            self.brightness = b

        def render(self, _canvas):  # noqa: ARG002 — stub
            pass

    _Eff.__name__ = name
    return _Eff


def _build_coord(*, intro_seconds=0.0, fade_seconds=0.05, idle_seconds=0.1,
                 messages=None, monkey=None):
    """Build a coordinator with stub everything; time.monotonic monkey is set by caller."""
    if monkey is None:
        monkey = pytest.MonkeyPatch()
    clock = _Clock()
    monkey.setattr(time, "monotonic", clock)
    fx_a = _make_effect("Honeycomb")()
    fx_b = _make_effect("NightSky")()
    heart = _make_effect("Heart")()

    coord_mod = importlib.import_module("lib_shared.effects_coordinator")

    msg_entries = list(messages or [])
    mgr = _StubMessageManager(
        messages=msg_entries,
        effects_settings=EffectsSettings(
            fade_seconds=fade_seconds,
            intro_seconds=intro_seconds,
            hold_seconds=10.0,
            idle_seconds=idle_seconds,
        ),
        text_settings=TextSettings(),
    )

    class _StubDisplay:
        width = 64
        height = 64

        def render(self, effect, scroller):  # noqa: ARG002 — stub
            return None

    class _StubScroller:
        text = ""

        def tick(self, width):  # noqa: ARG002 — stub
            return None

        def set_text(self, text, width):  # noqa: ARG002 — stub
            return None

        def set_brightness(self, brightness):  # noqa: ARG002 — stub
            return None

        def set_color(self, color):  # noqa: ARG002 — stub
            return None

        def set_speed(self, speed):  # noqa: ARG002 — stub
            return None

    coord = coord_mod.EffectsCoordinator(
        message_manager=mgr,
        display=_StubDisplay(),
        scroller=_StubScroller(),
        effects=[fx_a, fx_b],
        heart=heart,
    )
    return coord, clock, monkey, mgr


# --- selection log ---------------------------------------------------------


def test_selection_log_dumps_full_picked_message(caplog, monkeypatch):
    """A single INFO line at the out→in transition carries the picked
    message's id, sender, body, and media count alongside the chosen
    effect. Operators get the full context without cross-referencing
    multiple log lines.
    """
    picked_msg = Message(
        id="a7edac79-uuid-1",
        sender="+14152985015",
        body="hello world",
        received_at="2026-07-10T00:00:00Z",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/x.jpg"}],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )

    # Drive intro→out→in (selection fires at out→in).
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.005)
            coord.tick()

    selection_lines = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert selection_lines, "expected at least one 'Coordinator: selected' INFO line"
    line = selection_lines[0].getMessage()
    assert "a7edac79-uuid-1" in line, f"selected line missing msg_id: {line}"
    assert "+14152985015" in line, f"selected line missing sender: {line}"
    assert "hello world" in line, f"selected line missing body: {line}"
    assert "media=1" in line, f"selected line missing media count: {line}"
    # effect= is present — the actual effect name depends on whether
    # the coordinator picks MediaCycler (when media is non-empty) or
    # one of the rotation effects. We just need a non-empty token
    # after `effect=`.
    import re as _re
    m = _re.search(r"effect=(\S+)", line)
    assert m is not None, f"selected line missing effect= field: {line}"
    assert m.group(1), f"selected line had empty effect name: {line}"


def test_selection_log_emitted_once_per_transition(caplog, monkeypatch):
    """The selection log fires once per out→in transition, not per
    tick() — idempotent on the same picked entry."""
    picked_msg = Message(
        id="idem", sender="+1", body="hi",
        received_at="t", media=[],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.01)
            coord.tick()
    selection_count = sum(
        1 for r in caplog.records
        if "Coordinator: selected" in r.getMessage()
    )
    assert selection_count == 1, (
        f"expected exactly one selection log per transition; got {selection_count}"
    )


# --- fade-log consolidation ------------------------------------------------


def test_fade_out_log_is_single_line(caplog, monkeypatch):
    """`_begin_out` no longer emits a 3-arg multi-line INFO — the
    fade-out event is a single 'Coordinator: starting fade out' line
    carrying mode + effect + trigger."""
    picked_msg = Message(
        id="x", sender="+1", body="y", received_at="t", media=[],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.005)
            coord.tick()
    fade_out = [
        r for r in caplog.records
        if "starting fade out" in r.getMessage()
    ]
    assert fade_out, "expected a 'starting fade out' INFO line"
    line = fade_out[0].getMessage()
    assert "mode=" in line and "effect=" in line and "trigger=" in line, line


def test_fade_in_done_log_is_single_line(caplog, monkeypatch):
    """`in→<mode>` was the verbose 3-arg INFO 'Coordinator in→...';
    collapsed to single 'Coordinator: fade in done' line."""
    picked_msg = Message(
        id="x", sender="+1", body="y", received_at="t", media=[],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(600):
            clock.advance(0.005)
            coord.tick()
    fade_in_done = [
        r for r in caplog.records
        if "fade in done" in r.getMessage()
    ]
    assert fade_in_done, "expected a 'fade in done' INFO line"


def test_old_verbose_log_strings_absent(caplog, monkeypatch):
    """The previous verbose multi-arg log shapes ('Coordinator
    out→in:', 'Coordinator in→...', 'Coordinator background→out',
    'Coordinator._begin_out:') are gone — replaced by the simpler
    single-line forms."""
    picked_msg = Message(
        id="x", sender="+1", body="y", received_at="t", media=[],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.005)
            coord.tick()
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    for old in (
        "Coordinator out→in:",
        "Coordinator in→",
        "Coordinator background→out",
        "Coordinator._begin_out:",
    ):
        assert old not in all_text, (
            f"old verbose log shape '{old}' should be gone — got: {all_text[:200]}"
        )
