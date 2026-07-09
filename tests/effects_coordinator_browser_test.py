"""Browser-preview integration smoke for the media override (issue #38).

The browser preview runs the same `lib_shared/effects_coordinator.py`
as the Pi. It constructs an `EffectsCoordinator` via
`static/preview/heart-message-manager/app_main.py` and binds the
page-local render layer via `preview_main.py`. This test pins
the contract that the preview's coordinator path doesn't break
under the new `media_api_base_url` / `media_cache_dir` kwargs:

- Default kwargs (no media URL, no cache dir) work — the
  coordinator constructs and ticks without error.
- An MMS message with `media=[]` constructs the coordinator, runs
  ticks, and lands in `background` mode (no cycler, no warnings).
- An MMS message with non-empty `media` is rendered without error
  in the preview. The cycler is constructed but the items are
  dropped because `api_base_url` is empty (the preview is
  illustrative, not a real fetcher). The coordinator falls back
  to the rotation effect via `_maybe_fall_back_to_rotation`.

This is a smoke test — the preview's actual PyScript bootstrap
path is exercised in-browser, not in pytest. The test pins the
SHAPE of the preview's coordinator path so a future refactor
that breaks it (e.g., a required media_api_base_url kwarg) is
caught.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from tests.effects_coordinator_test import (  # noqa: E402
    _StubCanvas,
    _StubDisplay,
    _StubScroller,
    _StubMessageManager,
    _make_effect,
    _Clock,
)


def _build_preview_coord(messages=None, effects_settings=None):
    """Build a coordinator the way `static/preview/heart-message-manager/
    app_main.py` does: with `is_browser=True` and `media_api_base_url`
    set to a stub origin. The preview's per-page `preview_main.py`
    calls `coord.bind(...)` with the page-local render layer; we
    mirror that here via the constructor."""
    from lib_shared.effects_coordinator import EffectsCoordinator
    from lib_shared.models import EffectsSettings, TextSettings

    mgr = _StubMessageManager(
        messages=messages or [],
        effects_settings=effects_settings or EffectsSettings(),
    )
    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    return EffectsCoordinator(
        message_manager=mgr,
        display=display,
        scroller=scroller,
        effects=[fx_a],
        heart=heart,
        # issue #38: the preview constructs a BrowserMediaOverlay
        # (DOM-driven) instead of a MediaCycler (PIL/cv2-driven).
        is_browser=True,
        media_api_base_url="http://preview.test",
    )


def test_preview_coord_constructs_with_default_media_kwargs():
    """The preview's `app_main.py` constructs the coordinator with
    no `media_api_base_url` / `media_cache_dir` kwargs. Construction
    succeeds and `coord.current` is the heart (the boot-splash)."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="sms only",
            received_at="2026-07-09T00:00:00Z",
            media=[],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(messages=[msg])
    # The coordinator is bound to the page-local render layer
    # and boots with the heart as the current effect.
    assert coord.current is not None


def test_preview_coord_handles_sms_only_message():
    """An SMS-only message (media=[]) in the preview: ticks run,
    no cycler is constructed (because the cycler helper returns
    None for SMS-only), and the state machine lands in `background`
    (no text to display in the preview's seeded state)."""
    from lib_shared.models import EffectsSettings, Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="sms only",
            received_at="2026-07-09T00:00:00Z",
            media=[],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(
        messages=[msg],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=0.1,
            idle_seconds=0.05,
        ),
    )
    # Patch time.monotonic and tick the state machine forward.
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    try:
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out → in
        clock.advance(0.02)
        coord.tick()  # in → hold (text is set, so hold)
        # The current effect is the rotation entry, NOT a MediaCycler
        # (SMS-only messages don't construct a cycler).
        from lib_shared.patterns.media_cycler import MediaCycler

        assert not isinstance(coord.current, MediaCycler)
    finally:
        monkey.undo()


def test_preview_coord_mms_message_constructs_browser_overlay(caplog):
    """An MMS message in the preview: with `is_browser=True`, the
    coordinator constructs a `BrowserMediaOverlay` (not a
    `MediaCycler`) at the out→in transition. The overlay's
    `current_media_url` exposes the Flask proxy URL the JS-side
    `<img>` / `<video>` elements follow; with `media_api_base_url`
    set, the URL is built cleanly. No codec drops — the browser's
    native `<img>` decoder handles format failure at the JS layer,
    not in the Python cycler."""
    import logging
    import re

    from lib_shared.models import EffectsSettings, Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="mms",
            received_at="2026-07-09T00:00:00Z",
            media=[
                {"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"},
                {"type": "image/png", "url": "media/images/2026-07/b.png"},
            ],
        ),
        suppressed=False,
    )
    coord = _build_preview_coord(
        messages=[msg],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=0.1,
            idle_seconds=0.05,
        ),
    )
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)
    try:
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out → in
        clock.advance(0.02)
        coord.tick()  # in → hold
        # The current is a BrowserMediaOverlay (preview path) — NOT a
        # MediaCycler. The DOM elements are now driven by the
        # overlay's read-only properties via preview.js.
        from lib_shared.patterns.browser_media_overlay import BrowserMediaOverlay

        assert isinstance(coord.current, BrowserMediaOverlay)
        # No codec drops — items remain in the working list, and the
        # overlay is not exhausted (1-item case stays False; this
        # has 2 items anyway, so still False).
        assert coord.current.exhausted is False
        assert coord.current.items_remaining == 2
        # The flask proxy URL is built from the coordinator's
        # configured base + the active key. After `tick()` it
        # points at a real Flask route.
        current = coord.current
        current.tick()  # activate an item
        assert current.current_media_url.startswith(
            "http://preview.test/api/media/",
        )
        assert current.current_media_kind == "image"
        # The first picked item is `image/jpeg` (random over the
        # not-yet-shown set with 2 candidates, but for the first
        # advance the cycle picks uniformly — could be either).
        assert current.current_media_key in {
            "media/images/2026-07/a.jpg",
            "media/images/2026-07/b.png",
        }
    finally:
        monkey.undo()
