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


def _build_coord(*, intro_seconds=0.0, fade_seconds=0.05, hold_seconds=10.0,
                 idle_seconds=0.1, messages=None, monkey=None):
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
            hold_seconds=hold_seconds,
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


# --- round 2: pretty-print + media types + body-from-picked + cycler-noise dropped ---


def test_selection_log_pretty_prints_full_message_json(caplog, monkeypatch):
    """ISC-19: the `Coordinator: selected` log carries the FULL picked
    `Message.to_dict()` as a multi-line indented JSON block. The
    operator can read the entire selected record from one log
    record — no cross-referencing of buffer / set_text / out→in.
    """
    picked_msg = Message(
        id="2e384b3a-a3c8-4a5d-bda4-64da7a0440d7",
        sender="+14152985015",
        body="hello world",
        received_at="2026-07-10T03:36:08Z",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/x.jpg"}],
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

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert selected, "expected at least one selection log"
    text = selected[0].getMessage()
    # The summary line carries the operator's anchor fields.
    assert "2e384b3a-a3c8-4a5d-bda4-64da7a0440d7" in text
    assert "hello world" in text
    assert "+14152985015" in text
    # The JSON block carries every field of Message.to_dict().
    assert '"id"' in text
    assert '"sender"' in text
    assert '"body"' in text
    assert '"received_at"' in text
    assert '"media"' in text
    # Indented (multi-line) — assert at least 4-space indent on a
    # nested key (proves it's a pretty-print, not a single-line
    # `repr` of the dict).
    assert '    "id"' in text or '\n  "id"' in text, (
        f"expected indented JSON, got: {text[:300]}"
    )


def test_selection_log_includes_media_types_in_summary(caplog, monkeypatch):
    """ISC-20: the summary line carries `media=N (image/jpeg, ...)`
    so the operator knows WHAT KIND of media is being shown, not
    just how many. The follow-up question after `media=N` is
    "what type?" — answered on the same line.
    """
    picked_msg = Message(
        id="vid-1", sender="+1", body="",
        received_at="2026-07-10T03:36:08Z",
        media=[
            {"type": "video/3gpp", "url": "media/videos/2026-07/a.3gp"},
            {"type": "image/jpeg", "url": "media/images/2026-07/b.jpg"},
        ],
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

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert selected
    line = selected[0].getMessage().splitlines()[0]  # summary line only
    assert "media=2" in line, line
    assert "video/3gpp" in line, f"missing video type in summary: {line}"
    assert "image/jpeg" in line, f"missing image type in summary: {line}"


def test_fade_in_done_uses_picked_body_not_stale_last_shown(caplog, monkeypatch):
    """ISC-21: when a new MMS arrives with body='' (caption-less
    media), the `if text:` branch at line 893 is skipped, so
    `self.last_shown_text` keeps its value from the prior message.
    The fade-in-done log must read the actual picked body
    (`picked.message.body`) — not the stale `last_shown_text`. The
    live journal showed `text='With a pic!'` for an empty-body
    MMS because the scroller fallback was leaking into the log.
    """
    # First message: text-only, body='heya'. This sets
    # `last_shown_text='heya'` on the first cycle.
    first_msg = Message(
        id="m1", sender="+1", body="heya",
        received_at="2026-01-01T00:00:00Z", media=[],
    )
    # Second message: MMS with body='' (caption-less).
    second_msg = Message(
        id="m2", sender="+1", body="",
        received_at="2026-07-10T03:36:08Z",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/x.jpg"}],
    )
    first_view = MessageView(first_msg, source="mqtt", suppressed=False)
    second_view = MessageView(second_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, hold_seconds=0.05, idle_seconds=0.1,
        messages=[first_view], monkey=monkeypatch,
    )
    # Add the second message so the next fade picks it.
    mgr.add_message(second_view)

    with caplog.at_level(logging.INFO):
        coord.start()
        # Drive long enough to cycle through: intro → out → in
        # (m1) → hold (hold_seconds=0.05) → text_out → background
        # → idle (idle_seconds=0.1) → out → in (m2).
        for _ in range(1000):
            clock.advance(0.005)
            coord.tick()

    # Anchor on the m2 SELECTION log: find the index of the
    # selection record that mentions msg_id='m2', then take the
    # NEXT fade-in-done record. That's the m2 fade-in-done — not
    # the m1 re-roll that may come after m2 exhausts.
    records = list(caplog.records)
    m2_select_idx = next(
        (i for i, r in enumerate(records)
         if "Coordinator: selected" in r.getMessage() and "msg_id=m2" in r.getMessage()),
        None,
    )
    assert m2_select_idx is not None, "expected a selection log for m2"
    m2_fade_in_done = next(
        (r for r in records[m2_select_idx:] if "fade in done" in r.getMessage()),
        None,
    )
    assert m2_fade_in_done is not None, "expected a fade-in-done AFTER the m2 selection"
    line = m2_fade_in_done.getMessage()
    assert "body=''" in line, (
        f"expected body='' for the empty-body MMS, got stale value: {line!r}"
    )
    assert "heya" not in line, (
        f"stale 'heya' from the prior message leaked into the log: {line!r}"
    )


def test_cycler_fallback_info_lines_dropped(caplog, monkeypatch):
    """ISC-22: the three cycler-fallback INFO lines are gone —
    their info is already conveyed by the `starting fade out
    trigger=cycler_complete` line and the selection log's
    `(no picked entry — rotation)` annotation.
    """
    # A message with no media and no body — exercises the
    # "no picked entry" path. The selection log will mark it as
    # `(no picked entry — rotation)`.
    view = MessageView(
        Message(id="m1", sender="+1", body="", received_at="t", media=[]),
        source="mqtt", suppressed=False,
    )
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=0.05,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.01)
            coord.tick()
    all_text = "\n".join(r.getMessage() for r in caplog.records)
    for dropped in (
        "Coordinator media-cycler complete",
        "Coordinator media-cycler %s",
        "fading out for rotation",
        "no picked entry; rotation effect will run instead",
        "picked message has empty media; rotation effect will run",
    ):
        assert dropped not in all_text, (
            f"redundant cycler INFO line '{dropped}' should be gone — got: {all_text[:300]}"
        )


def test_fade_out_log_shows_last_shown_text(caplog, monkeypatch):
    """ISC-23: the `starting fade out` log shows
    `self.last_shown_text` (what was on the sign just before the
    fade-out), not `self.scroller.text` (which is the cleared
    scroller, always `''` at this point).
    """
    view = MessageView(
        Message(
            id="m1", sender="+1", body="goodbye world",
            received_at="2026-01-01T00:00:00Z", media=[],
        ),
        source="mqtt", suppressed=False,
    )
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, hold_seconds=0.05, idle_seconds=0.1,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        # Drive past intro→out→in→hold→text_out→background→idle
        # →out (this is the fade-out we want to inspect — the
        # initial intro fade-out is `last_text=''` by definition).
        for _ in range(800):
            clock.advance(0.005)
            coord.tick()
    fade_out = [
        r for r in caplog.records
        if "starting fade out" in r.getMessage()
    ]
    assert fade_out, "expected at least one 'starting fade out' INFO line"
    # The fade-out emitted AFTER the message was shown (so
    # `last_shown_text` is populated) must carry the actual body,
    # not `last_text=''`.
    last_shown = [r for r in fade_out if "last_text='goodbye world'" in r.getMessage()]
    assert last_shown, (
        f"expected a fade-out with last_text='goodbye world' — "
        f"got: {[r.getMessage() for r in fade_out]}"
    )
