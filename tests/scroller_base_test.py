"""Tests for lib_shared.scroller_base.ScrollerBase.

The base class is abstract; we subclass with stub hooks so we can exercise the
shared time/pixel logic without needing Pillow or rgbmatrix.
"""

import time
from unittest.mock import MagicMock

import pytest

# Ensure project root is on the path so lib_shared is importable
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib_shared.scroller_base import ScrollerBase


class _StubScroller(ScrollerBase):
    """Minimal concrete subclass — every char is 1 pixel wide, draws are no-ops."""

    def __init__(self, *args, char_width=1, **kwargs):
        super().__init__(*args, **kwargs)
        self._char_width = char_width
        self.draw_calls = []

    def measure_text(self, text):
        return len(text) * self._char_width

    def draw_text(self, canvas, text, x, y, color):
        self.draw_calls.append((text, x, y, color))

    def compute_layout(self, canvas_width, canvas_height):
        # 64x64: two lines, baselines at 16 and 48 (centered in each 64x32 half)
        self.single_line = canvas_height <= 32
        self.top_y = 16
        self.bottom_y = 48


def _make_time(monkeypatch, start=1000.0):
    """Replace time.monotonic() with a controllable clock.

    Returns a list-like `clock` where `clock.advance(s)` moves the clock
    forward by `s` seconds, and the next call to `time.monotonic()` returns
    the new time. The clock always starts at `start` so the first call
    returns `start` exactly.
    """
    state = {"t": start}
    clock = MagicMock()
    clock.advance = lambda s: state.update(t=state["t"] + s)
    clock.now = lambda: state["t"]
    monkeypatch.setattr(time, "monotonic", lambda: state["t"])
    return clock


def test_set_text_initializes_positions_and_text_width():
    s = _StubScroller(char_width=3)
    s.compute_layout(64, 64)
    s.set_text("hi", canvas_width=64)
    assert s.text == "hi"
    assert s.text_width == 6  # 2 chars * 3 px each
    assert s.top_x == 64
    assert s.bottom_x == 64


def test_tick_advances_top_x_by_expected_pixels(monkeypatch):
    """After 0.5s with frame_delay=0.05, top_x should drop by 10 pixels."""
    clock = _make_time(monkeypatch)
    s = _StubScroller(char_width=1)
    s.frame_delay = 0.05  # direct attr assignment; speed= kwarg is the public path
    s.compute_layout(64, 64)
    s.set_text("hi", canvas_width=64)
    initial_top = s.top_x
    clock.advance(0.5)
    s.tick(canvas_width=64)
    assert s.top_x == initial_top - 10


def test_tick_bottom_x_lags_top_x_by_offset_seconds(monkeypatch):
    """Within offset_seconds, bottom_x should not move; after, it catches up."""
    clock = _make_time(monkeypatch)
    s = _StubScroller(char_width=1)
    s.frame_delay = 0.05
    s.offset_seconds = 0.5
    s.compute_layout(64, 64)
    s.set_text("hi", canvas_width=64)
    initial_bot = s.bottom_x
    # Advance 0.2s — less than offset_seconds, so bottom_x should not move
    clock.advance(0.2)
    s.tick(canvas_width=64)
    assert s.bottom_x == initial_bot
    # Advance another 0.5s — well past offset, so bottom_x should drop
    clock.advance(0.5)
    s.tick(canvas_width=64)
    assert s.bottom_x < initial_bot


def test_tick_no_text_is_noop():
    s = _StubScroller()
    s.compute_layout(64, 64)
    # No set_text call. tick should not raise.
    s.tick(canvas_width=64)
    assert s.top_x == 0
    assert s.bottom_x == 0


def test_top_x_wraps_to_canvas_width_when_text_fully_off():
    """When top_x would drop past -text_width, it wraps back to canvas_width."""
    s = _StubScroller(char_width=1)
    s.frame_delay = 0.01
    s.compute_layout(64, 64)
    s.set_text("abc", canvas_width=64)  # text_width = 3
    s.top_x = -2  # 1 pixel away from end_x = -3
    s.start_time = 0.0
    s.last_frame = 0.0
    # Tick with a large elapsed time: should wrap back to canvas_width (64)
    with pytest.MonkeyPatch.context() as mp:
        state = {"t": 0.0}
        mp.setattr(time, "monotonic", lambda: (state.update(t=state["t"] + 1.0) or state["t"]))
        s.tick(canvas_width=64)
    assert s.top_x == 64


def test_render_no_text_does_not_call_draw():
    s = _StubScroller()
    s.compute_layout(64, 64)
    s.render(canvas=MagicMock())
    assert s.draw_calls == []


def test_render_two_lines_when_not_single_line():
    s = _StubScroller()
    s.compute_layout(64, 64)  # single_line = False
    s.set_text("hi", canvas_width=64)
    s.render(canvas=MagicMock())
    assert len(s.draw_calls) == 2
    # Top draw at top_y=16, bottom at bottom_y=48
    ys = sorted(call[2] for call in s.draw_calls)
    assert ys == [16, 48]


def test_render_single_line_renders_only_top():
    s = _StubScroller()
    s.compute_layout(64, 16)  # height <= 32, single_line = True
    s.set_text("hi", canvas_width=64)
    s.render(canvas=MagicMock())
    assert len(s.draw_calls) == 1
    assert s.draw_calls[0][2] == 16  # top_y was set to 16 by stub


def test_color_tuple_scales_by_brightness():
    s = _StubScroller(color=0xFF8040)
    s.set_brightness(0.5)
    r, g, b = s.color_tuple()
    # 0xFF * 0.5 = 127.5 -> 127; 0x80 * 0.5 = 64; 0x40 * 0.5 = 32
    assert r == 127
    assert g == 64
    assert b == 32


def test_set_text_with_bytes_decodes_utf8():
    s = _StubScroller()
    s.compute_layout(64, 64)
    s.set_text("hi".encode("utf-8"), canvas_width=64)
    assert s.text == "hi"


def test_set_text_emits_debug_log(caplog):
    """set_text was demoted from INFO to DEBUG in round 4
    (debug-visibility). The Pi's default LOG_LEVEL=INFO no longer
    surfaces every scroller.set_text call. After the round-4 log
    consolidation — selected-log carries msg + effect, starting
    fade out / fade in mark transitions — the scroller's internal
    set_text call is no longer an operator-visible event.

    DEBUG keeps it available for low-level scroller debugging
    when toggled at runtime. Operators grep the selected-log line
    for `what is on the sign?` diagnostics now, not the set_text
    line.
    """
    import logging

    s = _StubScroller()
    s.compute_layout(64, 64)
    # Lift the root logger to DEBUG so the lib_shared.scroller_base
    # logger (which defaults to WARNING) lets DEBUG records through.
    caplog.set_level(logging.DEBUG)
    s.set_text("hello world", canvas_width=64)

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("Scroller.set_text" in r.getMessage() and "hello world" in r.getMessage() for r in debug_records), (
        "scroller.set_text must emit a DEBUG log line (round 4 demoted it "
        "from INFO). Got: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # And the INFO-level stream must NOT see it (the operator's
    # default LOG_LEVEL=INFO journal stays quiet for this internal call).
    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert not any("Scroller.set_text" in r.getMessage() for r in info_records), (
        "scroller.set_text must NOT emit an INFO log (round 4 demoted it); "
        f"got: {[r.getMessage() for r in info_records]}"
    )


# --- text-scrim rectangle geometry (readability aid) -----------------------


def _scrim_scroller(*, scrim, text="hi", single_line=True):
    """A stub scroller with font metrics + text x/width set for scrim_rects()."""
    s = _StubScroller(speed=3)
    s.font_height = 15
    s.font_baseline = 12
    s.top_y = 38
    s.bottom_y = 10
    s.top_x = 10
    s.bottom_x = 40
    s.text_width = 20
    s.single_line = single_line
    s.text = text
    s.set_scrim(scrim)
    return s


def test_set_scrim_clamps():
    s = _StubScroller(speed=3)
    s.set_scrim(0.4)
    assert s.scrim == 0.4
    s.set_scrim(9)
    assert s.scrim == 1.0
    s.set_scrim(-3)
    assert s.scrim == 0.0
    s.set_scrim("bad")
    assert s.scrim == 0.0


def test_scrim_scales_with_brightness():
    """The effective scrim is the configured level times text brightness,
    so it fades in/out with the text (no lingering dark box)."""
    s = _StubScroller(speed=3)
    s.set_scrim(0.6)
    assert s.scrim == 0.6  # default brightness 1.0
    s.set_brightness(0.5)
    assert s.scrim == 0.3  # 0.6 * 0.5
    s.set_brightness(0.0)
    assert s.scrim == 0.0  # fully faded → no scrim


def test_scrim_rects_empty_when_faded_out():
    """With the text fully faded (brightness 0), no rects are produced even
    though the configured scrim level is non-zero."""
    s = _scrim_scroller(scrim=0.6)
    s.set_brightness(0.0)
    assert s.scrim_rects() == []


def test_scrim_rects_off_returns_empty():
    assert _scrim_scroller(scrim=0.0).scrim_rects() == []


def test_scrim_rects_no_text_returns_empty():
    assert _scrim_scroller(scrim=0.6, text="").scrim_rects() == []


def test_scrim_rects_single_line_geometry():
    """One rect hugging the text: x over [top_x, top_x+text_width], y glyph box."""
    rects = _scrim_scroller(scrim=0.6).scrim_rects()
    # top_x=10, text_width=20, x_pad=2 -> x0=8, x1=10+20+2=32
    # top_y=38 baseline, ascent=12, height=15, y_pad=1 -> y0=25, y1=38-12+15+1=42
    assert rects == [(8, 25, 32, 42)]


def test_scrim_rects_not_full_width():
    """The rect must NOT span the whole display width — only the text."""
    (x0, _y0, x1, _y1) = _scrim_scroller(scrim=0.6).scrim_rects()[0]
    # text_width(20) + 2*pad(2) = 24, far short of any full-panel width.
    assert x1 - x0 == 24


def test_scrim_rects_two_lines():
    """Two-line mode yields a rect per line, each at its own x."""
    rects = _scrim_scroller(scrim=0.6, single_line=False).scrim_rects()
    assert len(rects) == 2
    assert (8, 25, 32, 42) in rects  # top line (top_x=10, top_y=38)
    # bottom line: bottom_x=40 -> x0=38, x1=62 ; bottom_y=10 -> y0=-3, y1=14
    assert (38, -3, 62, 14) in rects


def test_scrim_rects_missing_font_metrics_returns_empty():
    """Without font_height set, no rect is produced (defensive)."""
    s = _StubScroller(speed=3)
    s.top_y = 38
    s.top_x = 10
    s.text_width = 20
    s.single_line = True
    s.text = "hi"
    s.set_scrim(0.6)
    # no font_height / font_baseline attributes set
    assert s.scrim_rects() == []
