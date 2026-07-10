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


def test_selection_log_carries_effect_name(caplog, monkeypatch):
    """Round 4 (debug-visibility): the `Coordinator: selected`
    log carries the picked message AND the effect that will render
    it on the SAME summary line. The operator's first journal
    record for any transition now answers "what is on the sign and
    how is it being rendered" — `msg_id=... body=... effect=...`
    plus the pretty-printed JSON of the full Message.

    The effect name resolves at the pick site via
    `_resolve_next_effect_name()`:
      - Media-bearing message → `MediaCycler` (host path)
      - Otherwise → `type(self.effects[(idx + 1) % len(effects)]).__name__`
        (the rotation entry that will own the next hold)
    """
    picked_msg = Message(
        id="r4-eff-test", sender="+1", body="round-4 single line",
        received_at="2026-07-10T05:00:00Z", media=[],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        # Drive to intro→out→in so the pick site runs.
        for _ in range(50):
            clock.advance(0.005)
            coord.tick()

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert selected, "expected at least one 'Coordinator: selected' INFO line"
    summary = selected[0].getMessage().splitlines()[0]
    assert "msg_id=r4-eff-test" in summary, summary
    assert "body='round-4 single line'" in summary, summary
    # Round 4: effect name lives on the SAME summary line —
    # `effect=Honeycomb`, `effect=NightSky`, etc.
    import re as _re
    m = _re.search(r"effect=(\S+)", summary)
    assert m is not None, (
        f"round 4 dropped the effect= field from the selected-log's "
        f"summary line; got: {summary}"
    )
    effect_name = m.group(1)
    assert effect_name in ("Honeycomb", "NightSky"), (
        f"unexpected effect name for a non-media message; "
        f"expected one of the rotation effects, got: {effect_name!r}"
    )


def test_selection_log_carries_media_cycler_effect(caplog, monkeypatch):
    """Round 4: a media-bearing message resolves to MediaCycler
    (or BrowserMediaOverlay on the preview) on the selected-log
    summary line. Picks the effect name at the pick site instead
    of deferring to out→in (where idx advances)."""
    picked_msg = Message(
        id="r4-cycler", sender="+1", body="",
        received_at="2026-07-10T05:00:00Z",
        media=[{"type": "image/jpeg", "url": "media/images/2026-07/pic.jpg"}],
    )
    view = MessageView(picked_msg, source="mqtt", suppressed=False)
    coord, clock, _monkey, _mgr = _build_coord(
        intro_seconds=0.0, fade_seconds=0.02, idle_seconds=1e9,
        messages=[view], monkey=monkeypatch,
    )
    with caplog.at_level(logging.INFO):
        coord.start()
        for _ in range(50):
            clock.advance(0.005)
            coord.tick()

    selected = [r for r in caplog.records if "Coordinator: selected" in r.getMessage()]
    assert selected
    summary = selected[0].getMessage().splitlines()[0]
    assert "effect=MediaCycler" in summary, (
        f"media-bearing pick should resolve to MediaCycler at the "
        f"pick site; got: {summary}"
    )


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


def test_fade_in_done_log_dropped(caplog, monkeypatch):
    """Round 4 (debug-visibility): the `fade in done` log was
    dropped entirely. Internal fade-mechanics — the operator reads
    the picked message + effect from the `Coordinator: selected`
    log and the transition from `starting fade out` / `starting
    fade in`. A third "done" line is redundant."""
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
    assert not fade_in_done, (
        f"round 4 dropped the 'fade in done' log; got: "
        f"{[r.getMessage() for r in fade_in_done]}"
    )


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


def test_fade_in_done_and_hold_text_out_logs_dropped(caplog, monkeypatch):
    """Round 4 (debug-visibility): the per-cycle `fade in done`,
    `hold→text_out`, and `text_out→background` logs are dropped
    entirely. They conveyed timing/internal state between the
    picked message and the next picked message — operator reads
    the next selected-log for that, not transient fade-stepping
    notices. This test pins the absense of all three strings.
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

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    for dropped in (
        "fade in done",
        "hold→text_out",
        "text_out→background",
        # Round 4 also drops the hold-interrupt (new id) line —
        # the new queue + next-pick mechanism replaced the
        # interrupt, so the log line is gone too.
        "hold interrupt (new id)",
    ):
        assert dropped not in all_text, (
            f"round 4 dropped '{dropped}'; got full text:\n{all_text[:800]}"
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
