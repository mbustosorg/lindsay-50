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


def test_set_text_emits_info_log(caplog):
    """set_text must log at INFO (not DEBUG) so Pi operators see scroller-text
    changes in journalctl without toggling LOG_LEVEL.

    The Pi is running in production with LOG_LEVEL=INFO; if this log line
    drops back to DEBUG, every "what is the sign showing right now?"
    diagnostic has to wait for a service restart that flips LOG_LEVEL. With
    an INFO log here, every coordinator-driven text change (out→in on a
    fresh message, hold/background interrupt by a new SMS) is one
    `journalctl -u lindsay_50 -f` grep away.
    """
    import logging

    s = _StubScroller()
    s.compute_layout(64, 64)
    # Set root logger to INFO so the lib_shared.scroller_base logger
    # (which defaults to WARNING with no propagate=False) lets INFO
    # records through. caplog's handler is attached to root by default.
    caplog.set_level(logging.INFO)
    s.set_text("hello world", canvas_width=64)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "Scroller.set_text" in r.getMessage() and "hello world" in r.getMessage()
        for r in info_records
    ), (
        "scroller.set_text must emit an INFO log line naming the new text. "
        f"Got: {[r.getMessage() for r in caplog.records]}"
    )
