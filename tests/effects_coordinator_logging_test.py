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
    """Round 3 (debug-visibility): the `Coordinator: selected` log
    fires at the pick site (before fade-out) and carries the
    picked message's id, sender, body, and media count. The
    `effect=` field moved to the `starting fade in` log because
    we don't know which rotation effect will be active until
    out→in (where idx advances). Operators get the full message
    context from one log record.
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
    # The effect name is in the `starting fade in` log, not the
    # selected-log (effect resolves at out→in when idx advances).
    fade_in = [r for r in caplog.records if "starting fade in" in r.getMessage()]
    assert fade_in, "expected a 'starting fade in' INFO log"
    import re as _re
    m = _re.search(r"effect=(\S+)", fade_in[0].getMessage())
    assert m is not None, f"fade-in log missing effect= field: {fade_in[0].getMessage()}"
    assert m.group(1), f"fade-in log had empty effect name: {fade_in[0].getMessage()}"


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


def test_fade_in_done_log_has_no_body(caplog, monkeypatch):
    """Round 3 (debug-visibility): the `body=` field is gone from
    the `fade in done` log entirely — the selected-log at the pick
    site already carried the body, so re-logging it at fade-in-done
    just duplicates. This replaces the round-2 ISC-21 test (which
    pinned the `body=` field's value to be non-stale); the underlying
    bug is fixed by removing the field, not by correcting the read.
    """
    first_msg = Message(
        id="m1", sender="+1", body="heya",
        received_at="2026-01-01T00:00:00Z", media=[],
    )
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
    mgr.add_message(second_view)

    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(1000):
            clock.advance(0.005)
            coord.tick()

    fade_in_done_records = [r for r in caplog.records if "fade in done" in r.getMessage()]
    assert fade_in_done_records, "expected at least one fade-in-done log"
    for r in fade_in_done_records:
        msg = r.getMessage()
        assert "body=" not in msg, (
            f"fade-in-done log should not carry body= (round 3 dropped it); got: {msg!r}"
        )
        assert "heya" not in msg, (
            f"fade-in-done log leaked 'heya' from prior cycle; got: {msg!r}"
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


def test_fade_out_log_has_no_last_text(caplog, monkeypatch):
    """Round 3 (debug-visibility): the `last_text=` field is gone
    from the `starting fade out` log entirely. The body of the
    message that was on the sign is the job of the previous
    cycle's `Coordinator: selected` log; the fade-out log carries
    effect + trigger only. This replaces the round-2 ISC-23 test
    (which pinned the `last_text=` field's value to be non-stale);
    the underlying bug is fixed by removing the field, not by
    correcting the read.
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
        for _ in range(800):
            clock.advance(0.005)
            coord.tick()
    fade_out = [r for r in caplog.records if "starting fade out" in r.getMessage()]
    assert fade_out, "expected at least one 'starting fade out' INFO line"
    for r in fade_out:
        msg = r.getMessage()
        assert "last_text=" not in msg, (
            f"fade-out log should not carry last_text= (round 3 dropped it); got: {msg!r}"
        )
        assert "goodbye world" not in msg, (
            f"fade-out log leaked 'goodbye world' from the prior cycle; got: {msg!r}"
        )


# --- round 3: ordering — selected fires BEFORE fade-out ---------------------


def test_selected_log_fires_before_fade_out_log(caplog, monkeypatch):
    """Round 3 contract: the `Coordinator: selected` log fires BEFORE
    the `starting fade out` log in the same transition. The operator
    reading a journal tail sees the picked message context FIRST, then
    the fade-out event — not the reverse. This is the order the user
    asked for: 'the first thing that appears should be the selected
    message.'
    """
    view = MessageView(
        Message(id="order-test", sender="+1", body="first appearance",
                received_at="t", media=[]),
        source="mqtt", suppressed=False,
    )
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(400):
            clock.advance(0.005)
            coord.tick()

    # Find the indices of the first 'Coordinator: selected' and the
    # first 'starting fade out' log records. The selected one must
    # come first.
    selected_idx = None
    fade_out_idx = None
    for i, r in enumerate(caplog.records):
        msg = r.getMessage()
        if selected_idx is None and "Coordinator: selected" in msg:
            selected_idx = i
        if fade_out_idx is None and "starting fade out" in msg:
            fade_out_idx = i
    assert selected_idx is not None, (
        f"expected a 'Coordinator: selected' log; got: "
        f"{[r.getMessage()[:80] for r in caplog.records[:5]]}"
    )
    assert fade_out_idx is not None, (
        f"expected a 'starting fade out' log; got: "
        f"{[r.getMessage()[:80] for r in caplog.records[:5]]}"
    )
    assert selected_idx < fade_out_idx, (
        f"'Coordinator: selected' must fire BEFORE 'starting fade out' "
        f"in the same transition; got selected_idx={selected_idx} "
        f"fade_out_idx={fade_out_idx}. Records in order: "
        f"{[r.getMessage()[:80] for r in caplog.records[:selected_idx + 3]]}"
    )
