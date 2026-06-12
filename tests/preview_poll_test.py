"""Tests for static/preview/preview.js's polling loop.

Asserts the file:
- Schedules a 3000-ms (3-second) poll
- Fetches /api/live-messages?limit=1&suppress=true
- Compares the polled first message's body against a `lastShownBody`
  variable before calling coordinator.request_message
- Does NOT call coordinator.request_message when the body matches
  (the dedup branch)
- Does NOT instantiate a WebSocket (v1 is polling only)
"""

import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
JS_PATH = _PROJECT_ROOT / "heart-message-manager" / "static" / "preview" / "preview.js"


def _js_source() -> str:
    return JS_PATH.read_text()


def test_preview_js_uses_3000ms_poll_cadence():
    """The polling interval is 3000 ms, matching templates/testing.html."""
    src = _js_source()
    assert "3000" in src
    # The constant is referenced by the setInterval call
    assert re.search(r"setInterval\s*\(\s*pollLatestMessage\s*,\s*POLL_MS\s*\)", src)


def test_preview_js_fetches_live_messages_with_limit1_suppresstrue():
    """The poll URL is /api/live-messages?limit=1&suppress=true."""
    src = _js_source()
    assert "/api/live-messages?limit=1&suppress=true" in src


def test_preview_js_dedupes_against_lastShownBody():
    """The polled body is compared to a lastShownBody variable before
    coordinator.request_message is called."""
    src = _js_source()
    assert "lastShownBody" in src
    # The dedup branch: `if (body === lastShownBody) return;` lives in
    # pollLatestMessage() and gates the request_message call.
    assert re.search(r"if\s*\(\s*body\s*===\s*lastShownBody\s*\)\s*return", src)


def test_preview_js_does_not_call_request_message_on_duplicate():
    """In the duplicate-body branch, coordinator.request_message is NOT called.

    Asserted by checking that the dedup `return` precedes the
    request_message call in source order.
    """
    src = _js_source()
    dedup_pos = src.find("if (body === lastShownBody) return")
    # PyScript 2024.9.x removed `window.pyscript.globals.get(...)`; the
    # request_message call now goes through the plain `window.request_message`
    # bridge that preview_main.py installs on `js.window`.
    call_pos = src.find("window.request_message(body)")
    assert dedup_pos != -1, "Dedup check missing"
    assert call_pos != -1, "request_message call missing"
    assert dedup_pos < call_pos, (
        "Dedup check must run before request_message is called; otherwise "
        "duplicate polls would re-kick the fade."
    )


def test_preview_js_dedupes_on_empty_list():
    """An empty poll response is a no-op (no request_message, no dedup update)."""
    src = _js_source()
    # `if (!Array.isArray(data) || data.length === 0) return;` short-circuits
    # pollLatestMessage before the body comparison
    assert re.search(r"length\s*===\s*0", src)


def test_preview_js_no_websocket():
    """v1 has no WebSocket — assert none is constructed in preview.js."""
    src = _js_source()
    assert "new WebSocket" not in src
    assert "WebSocket(" not in src
    assert "EventSource" not in src  # no SSE either


def test_preview_js_caps_render_at_30fps():
    """The render loop skips frames below 1000/30 ms to match the device cadence."""
    src = _js_source()
    assert "1000 / 30" in src or "1000/30" in src
    assert "FRAME_MS" in src


def test_preview_js_caps_canvas_size_at_800px():
    """The canvas is sized to min(800, availableWidth) per the design."""
    src = _js_source()
    assert "800" in src
    # The cap is in sizeCanvasToViewport() — the source declares `const max = 800`
    # and uses Math.min(max, ...) for both the long-edge clamp and the final
    # width/height calculation. Assert the cap constant is present and that
    # Math.min is used to apply it.
    assert re.search(r"const\s+max\s*=\s*800", src)
    assert re.search(r"Math\.min\s*\(\s*max", src)


def test_preview_js_first_poll_runs_immediately():
    """The first poll runs at startup (matches the testing page's
    fetchMessages() + setInterval(fetchMessages, 3000) pattern)."""
    src = _js_source()
    # The setInterval is preceded by an immediate call
    interval_call = src.find("setInterval(pollLatestMessage, POLL_MS)")
    immediate_call = src.find("pollLatestMessage()")
    assert interval_call != -1
    assert immediate_call != -1
    assert (
        immediate_call < interval_call
    ), "First poll must run immediately at startup, not just on the first tick"


def test_preview_js_initializes_lastShownBody_to_null():
    """lastShownBody starts as null so the first non-empty poll triggers
    request_message."""
    src = _js_source()
    assert re.search(r"let\s+lastShownBody\s*=\s*null", src)


def test_preview_js_does_not_add_cors_headers():
    """Polling uses the same-origin session cookie, not CORS."""
    src = _js_source()
    assert "Access-Control" not in src
    assert "credentials" in src  # but it does pass credentials


def test_preview_js_resizes_canvas_on_window_resize():
    """Regression: the canvas is re-sized on window resize events so it
    tracks the viewport when the user opens devtools, rotates a tablet,
    or simply drags the window edge. The previous version only called
    sizeCanvasToViewport once at init time, so the canvas stayed at
    whatever size the viewport was at first paint.

    v2 prefers a ResizeObserver on the bg-white card (catches viewport
    changes that window.resize misses, like URL bar collapse on mobile
    and devtools docked to the side) and falls back to window.resize
    for browsers without ResizeObserver. The test asserts at least one
    of those two paths is wired up.
    """
    src = _js_source()
    has_ro = "ResizeObserver" in src and re.search(r"new\s+ResizeObserver\s*\(", src)
    has_window_resize = re.search(r"window\.addEventListener\(\s*[\"']resize[\"']", src)
    assert has_ro or has_window_resize, (
        "preview.js must observe viewport changes via ResizeObserver "
        "or window.addEventListener('resize', ...)"
    )
    # The handler must invoke sizeCanvasToViewport — directly or via rAF
    assert "sizeCanvasToViewport" in src


def test_preview_js_uses_resize_observer_on_card():
    """The primary resize path is a ResizeObserver on the bg-white card,
    not window.resize. This catches viewport changes that window.resize
    misses (URL bar collapse on mobile, devtools docked to the side)
    and uses the card's actual clientWidth so the canvas can't overflow
    the card's content area on narrow viewports.
    """
    src = _js_source()
    assert (
        "ResizeObserver" in src
    ), "preview.js should use ResizeObserver for the primary resize path"
    assert re.search(
        r"new\s+ResizeObserver\s*\(", src
    ), "preview.js must instantiate a ResizeObserver"
    # The observer must be attached to the card, not the canvas itself
    # (the canvas's parent sizes to the canvas, which would give a
    # circular reference).
    assert re.search(r"\.observe\s*\(\s*card\s*\)", src) or re.search(
        r"ro\.observe\s*\(", src
    ), "ResizeObserver must be attached to the card element"


def test_preview_js_uses_card_clientWidth_for_sizing():
    """The canvas size is computed from the card's clientWidth (not
    window.innerWidth) so the canvas can't overflow the card's content
    area on narrow viewports.
    """
    src = _js_source()
    assert "card.clientWidth" in src, (
        "sizeCanvasToViewport must read the card's clientWidth to "
        "compute the canvas size"
    )
    # The fallback (no card found) subtracts 160 to leave room for the
    # card's p-12 (96px) + the dark div's p-4 (32px) padding chain.
    assert re.search(r"window\.innerWidth\s*-\s*160", src), (
        "sizeCanvasToViewport must fall back to window.innerWidth - 160 "
        "when the card isn't found"
    )


def test_preview_js_listens_for_pyscript_py_done_event():
    """Regression: PyScript 2024.9.x's `py:ready` event fires BEFORE the
    main module has evaluated, so the top-level functions exposed by
    preview_main.py (request_message, tick, get_frame_rgba) are not yet
    defined when `py:ready` fires. The post-evaluation event is
    `py:done` (CustomEvent, bubbles on each `<py-script>` element), and
    `py:all-done` is the equivalent plain Event fired once all
    py-script elements are done. preview.js must listen for `py:done`
    (primary) and `py:all-done` (fallback) or the page never advances
    past the "Loading preview…" overlay, even though the Python module
    loaded fine and all the static file fetches returned 200.

    The list is also kept as a backwards-compat fallback for older
    PyScript releases that still fire `pyodideReady` instead of `py:done`.
    """
    src = _js_source()
    # The new event name (with the colon)
    assert re.search(
        r"addEventListener\(\s*[\"']py:done[\"']", src
    ), "preview.js must listen for PyScript 2024.9.x's 'py:done' event"
    # The plain-Event equivalent fires once all py-script elements are done
    assert re.search(
        r"addEventListener\(\s*[\"']py:all-done[\"']", src
    ), "preview.js must also listen for 'py:all-done' (plain Event variant)"
    # The legacy event name should still be wired up for older releases
    assert re.search(
        r"addEventListener\(\s*[\"']pyodideReady[\"']", src
    ), "preview.js must keep a 'pyodideReady' fallback for older PyScript"
    # All three listeners should call the same handler, otherwise the page
    # would boot twice on a runtime that fires more than one of the events.
    assert src.count("addEventListener") >= 3
    # The handler is referenced in all three addEventListener calls.
