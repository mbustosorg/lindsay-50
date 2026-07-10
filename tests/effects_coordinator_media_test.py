"""Tests for the EffectsCoordinator's MMS media override (issue #38).

When the picked message has a non-empty `media` list, the coordinator
constructs a `MediaCycler` at the out→in transition and assigns it
to `self.current` in place of the rotation effect. The cycler takes
over for the duration of the hold; on `exhausted` (every attachment
failed to decode or the list ran out), the coordinator falls back to
`self.effects[self.idx]` for the remainder of the hold.

This file pins:
- SMS-only messages: `self.current` is a rotation effect (no cycler)
- MMS messages: `self.current` is a `MediaCycler` after the out→in transition
- Cycler falls back to the rotation effect when `exhausted` flips on
- The cycler receives the correct `api_base_url` and `cache_dir`
- `get_display_message` populates `_last_picked_entry` for the cycler
"""

from __future__ import annotations

import hashlib
import io
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Re-use stubs from effects_coordinator_test for a single source of truth
# ---------------------------------------------------------------------------

from tests.effects_coordinator_test import (  # noqa: E402
    _StubDisplay,
    _StubScroller,
    _StubMessageManager,
    _make_effect,
    _Clock,
)


def _make_png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (8, 8), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_image(path: Path) -> None:
    path.write_bytes(_make_png_bytes())


def _prep_cache_for_jpeg(cache_dir: Path, key: str) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    p = cache_dir / f"{digest}.jpg"
    _write_image(p)
    return p


# ---------------------------------------------------------------------------
# Helper: build a coordinator with a controllable media API base URL + cache
# ---------------------------------------------------------------------------


def _build_coord(
    *,
    message_manager,
    media_api_base_url: str = "http://test",
    media_cache_dir: str = "",
    monkeypatch=None,
):
    from lib_shared.effects_coordinator import EffectsCoordinator

    if monkeypatch is not None:
        _patch_config(monkeypatch)

    display = _StubDisplay()
    scroller = _StubScroller()
    fx_a = _make_effect("A")()
    heart = _make_effect("Heart")()
    return EffectsCoordinator(
        message_manager=message_manager,
        display=display,  # type: ignore[arg-type]
        scroller=scroller,  # type: ignore[arg-type]
        effects=[fx_a],  # type: ignore[arg-type]
        heart=heart,  # type: ignore[arg-type]
        media_api_base_url=media_api_base_url,
        media_cache_dir=media_cache_dir,
    )


def _patch_config(monkeypatch):
    """Patch `get_config` so ImageDisplay (used by the cycler) can
    construct against the host's missing TOML config. image_display
    imports get_config at module load — patch both the source and
    the consumer's globals to handle the case where image_display
    was imported before the patch is applied."""
    cfg_stub = SimpleNamespace(
        if_exists=lambda k: {"PNG_INTERVAL": "0.05", "PNG_FADE": "0.02"}.get(k),
    )

    def _stub_get_config(required_keys=None):  # required_keys: signature match
        # Mark `required_keys` as intentionally unused at runtime —
        # the real get_config has the same signature, callers ignore it.
        _ = required_keys
        return cfg_stub

    monkeypatch.setattr(
        "lib_shared.config_reader.get_config",
        _stub_get_config,
    )
    try:
        import lib_shared.patterns.image_display as _image_mod
    except ImportError:
        _image_mod = None
    if _image_mod is not None:
        monkeypatch.setattr(_image_mod, "get_config", _stub_get_config)


@pytest.fixture(autouse=True)
def auto_patch_config_fixture(monkeypatch):
    """Auto-patch get_config for every test in this module.

    Most tests in this file construct a MediaCycler somewhere
    (directly or via the coordinator), and the cycler constructs
    ImageDisplay inner renderers, which call get_config. Without
    this autouse patch, ImageDisplay raises "Initial call requires
    required_keys" because no settings.toml is loaded in the host
    test environment."""
    _patch_config(monkeypatch)
    yield monkeypatch


# ---------------------------------------------------------------------------
# get_display_message side-channel: _last_picked_entry
# ---------------------------------------------------------------------------


def test_get_display_message_populates_last_picked_entry():
    """After a `get_display_message()` call, `_last_picked_entry` is
    set to the picked `MessageView`. The out→in transition uses
    this to read the picked message's `media` list."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(id="m1", sender="+15551234567", body="hi", received_at="2026-07-09T00:00:00Z", media=[]),
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord = _build_coord(message_manager=mgr)

    # Before any call, _last_picked_entry is None.
    assert coord._last_picked_entry is None
    # After a call, it's set to the picked MessageView.
    coord.get_display_message()
    assert coord._last_picked_entry is not None
    assert coord._last_picked_entry.message.id == "m1"


def test_get_display_message_resets_last_picked_entry_on_empty_buffer():
    """`get_display_message()` resets `_last_picked_entry` to None at
    the start of every call. An empty buffer leaves it None."""
    mgr = _StubMessageManager(messages=[])
    coord = _build_coord(message_manager=mgr)
    assert coord.get_display_message() is None
    assert coord._last_picked_entry is None


# ---------------------------------------------------------------------------
# _maybe_build_media_cycler: SMS-only messages → no cycler
# ---------------------------------------------------------------------------


def test_maybe_build_media_cycler_returns_none_for_sms_only():
    """An SMS-only message (media=[]) returns None — no cycler, the
    rotation effect takes the fade-in."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(id="m1", sender="+15551234567", body="sms only", received_at="2026-07-09T00:00:00Z", media=[]),
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    coord = _build_coord(message_manager=mgr)
    coord.get_display_message()  # populates _last_picked_entry
    assert coord._maybe_build_media_cycler() is None


def test_maybe_build_media_cycler_returns_none_when_no_picked_entry():
    """If no pick has happened yet, the helper returns None (the
    intro→out seed path doesn't pick)."""
    mgr = _StubMessageManager(messages=[])
    coord = _build_coord(message_manager=mgr)
    # Don't call get_display_message — _last_picked_entry stays None.
    assert coord._last_picked_entry is None
    assert coord._maybe_build_media_cycler() is None


# ---------------------------------------------------------------------------
# _maybe_build_media_cycler: MMS messages → cycler
# ---------------------------------------------------------------------------


def test_maybe_build_media_cycler_constructs_for_mms_message(tmp_path):
    """An MMS message with a non-empty `media` list produces a
    `MediaCycler`. The cycler is bound to the coordinator's display,
    the configured `api_base_url`, and the configured `cache_dir`."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="an mms",
            received_at="2026-07-09T00:00:00Z",
            media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        ),
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    cache_dir = tmp_path / "media-cache"
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/a.jpg")
    coord = _build_coord(
        message_manager=mgr,
        media_api_base_url="http://flask.test",
        media_cache_dir=str(cache_dir),
    )
    coord.get_display_message()  # populates _last_picked_entry

    cycler = coord._maybe_build_media_cycler()
    assert cycler is not None
    from lib_shared.patterns.media_cycler import MediaCycler

    assert isinstance(cycler, MediaCycler)
    # The cycler carries the picked message's id and the media list.
    assert cycler.message_id == "m1"
    assert cycler._api_base_url == "http://flask.test"
    assert str(cycler._cache_dir) == str(cache_dir)


def test_maybe_build_media_cycler_uses_default_cache_dir_when_unset():
    """An empty `media_cache_dir` falls back to the OS temp dir
    (the cycler's own default) — the coordinator doesn't force a
    cache location on operators who haven't set one."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="an mms",
            received_at="2026-07-09T00:00:00Z",
            media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        ),
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    # Pre-populate the OS tempdir cache so the cycler can build
    # without dropping the item on a missing-file path.
    import tempfile

    tmp_cache = Path(tempfile.gettempdir()) / "lindsay-50-media"
    _prep_cache_for_jpeg(tmp_cache, "media/images/2026-07/a.jpg")
    try:
        coord = _build_coord(message_manager=mgr, media_cache_dir="")
        coord.get_display_message()

        cycler = coord._maybe_build_media_cycler()
        assert cycler is not None
        # The cycler's default cache dir is "{tempdir}/lindsay-50-media"
        # — the coordinator passing cache_dir=None (via "" → None) lets
        # the cycler pick that.
        assert "lindsay-50-media" in str(cycler._cache_dir)  # type: ignore[attr-defined]
    finally:
        # Best-effort cleanup; the cache file may have a different
        # sha256 if tests run with different keys. We don't want
        # the OS temp dir polluted between runs.
        for p in tmp_cache.glob("*"):
            try:
                p.unlink()
            except OSError:
                pass


def test_maybe_fall_back_to_rotation_keeps_cycler_when_not_exhausted(tmp_path):
    """A `MediaCycler` with `exhausted=False` is left in place — the
    cycler keeps running until `hold_seconds` elapses (or the
    coordinator's other cutoff paths fire)."""
    from lib_shared.models import Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="mms",
            received_at="2026-07-09T00:00:00Z",
            media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        ),
        suppressed=False,
    )
    mgr = _StubMessageManager(messages=[msg])
    cache_dir = tmp_path / "media-cache"
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/a.jpg")
    coord = _build_coord(message_manager=mgr, media_cache_dir=str(cache_dir))
    coord.get_display_message()
    fx_a = _make_effect("A")()
    coord.effects = [fx_a]
    coord.idx = 0

    cycler = coord._maybe_build_media_cycler()
    assert cycler is not None
    # Cycler has 1 item and is not exhausted (1-item case stays False).
    assert cycler.exhausted is False  # type: ignore[attr-defined]
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Cycler still in place.
    assert coord.current is cycler


# ---------------------------------------------------------------------------
# _maybe_fall_back_to_rotation: cycler exhausted → rotation effect
# ---------------------------------------------------------------------------


def test_maybe_fall_back_to_rotation_noop_for_non_cycler():
    """When `self.current` is a normal Effect (not a MediaCycler),
    the helper is a no-op. This is the common case — most messages
    are SMS-only and never get a cycler."""
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.current = _make_effect("A")()
    initial = coord.current
    coord._maybe_fall_back_to_rotation()
    assert coord.current is initial


def test_maybe_fall_back_to_rotation_triggers_fade_when_cycler_exhausted():
    """A `MediaCycler` with `exhausted=True` triggers the existing
    fade-out machinery — `self.current` stays as the cycler, and
    `self.mode` flips to `"out"` so `_step_fade` ramps the cycler's
    brightness to 0 on the next tick. The rotation effect swap
    happens later, when the `out` mode's fade completes (driven
    by the live `tick()` flow, not the helper)."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a, fx_b]
    coord.idx = 1
    coord.mode = "hold"

    # Build a cycler and mark it exhausted.
    cycler = MediaCycler(
        "m1",
        [{"type": "image/jpeg", "url": "key/a.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    # Force the cycler into the exhausted state (D12: every item dropped).
    cycler.exhausted = True
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Cycler still in place; fade-out triggered via the existing
    # `out` mode machinery — `_step_fade` drives the ramp on the
    # next tick, `out`-mode-completion does the rotation swap.
    assert coord.current is cycler
    assert coord.mode == "out"


def test_maybe_fall_back_to_rotation_triggers_fade_when_cycler_complete():
    """A `MediaCycler` with `complete=True` triggers the existing
    fade-out machinery — same path as `exhausted`. The cycler
    played through its content (1-item ran for `duration` seconds;
    multi-item cycled every attachment); the coordinator fades the
    cycler to black and the rotation effect fades in for the next
    cycle instead of looping the same frame."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a, fx_b]
    coord.idx = 1
    coord.mode = "hold"

    cycler = MediaCycler(
        "m2",
        [{"type": "image/jpeg", "url": "key/b.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    # `complete` is set when the cycler decides it's done — we
    # flip it manually here so the test doesn't have to drive the
    # elapsed-time path.
    cycler.complete = True
    cycler.exhausted = False  # mutually independent; cover both shapes
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Cycler still in place; fade-out triggered (mode flipped to
    # `out`). The actual rotation swap happens later in the live
    # `tick()` flow when the fade completes — covered by the
    # `_fade_completes_swaps_to_rotation_effect` end-to-end test
    # below.
    assert coord.current is cycler
    assert coord.mode == "out"


def test_maybe_fall_back_to_rotation_clears_last_picked_entry():
    """The fade-out trigger clears `_last_picked_entry` so the
    `out` mode's MediaCycler rebuild at fade-complete returns None
    — we want the rotation effect to take over, not a fresh cycler
    for the same message that just finished playing."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0
    coord.mode = "hold"
    # Pretend the cycler was built off a picked message — set the
    # side-channel sentinel so we can verify it's cleared.
    coord._last_picked_entry = "stale"  # type: ignore[assignment]

    cycler = MediaCycler(
        "m3",
        [{"type": "image/jpeg", "url": "key/c.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    cycler.complete = True
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Picked entry cleared so the out-mode cycler rebuild at
    # fade-complete returns None.
    assert coord._last_picked_entry is None


def test_maybe_fall_back_to_rotation_idempotent_in_out_mode():
    """Once the helper has flipped mode to `out`, subsequent calls
    are no-ops — the live `out` mode machinery is driving the fade,
    and re-entering would just restart the fade clock."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0
    coord.mode = "out"  # already mid-fade-out

    cycler = MediaCycler(
        "m4",
        [{"type": "image/jpeg", "url": "key/d.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    cycler.complete = True
    coord.current = cycler

    # Capture fade_start before — it should be unchanged after a
    # second helper call (helper is a no-op while mode is `out`).
    coord.fade_start = 12345.0
    coord._maybe_fall_back_to_rotation()
    assert coord.fade_start == 12345.0


def test_maybe_fall_back_to_rotation_idempotent_in_in_mode():
    """The cycler was just swapped in by an out→in transition
    (mode is `in`, brightness climbing back to 1.0). Firing
    another fade-out mid fade-in would oscillate — bail; the
    cycler's `complete` / `exhausted` flags stay set, so the
    next tick in `hold` / `background` will pick up the fade."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0
    coord.mode = "in"  # mid fade-in

    cycler = MediaCycler(
        "m5",
        [{"type": "image/jpeg", "url": "key/e.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    cycler.complete = True
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Mode stayed `in` — helper refused to retrigger.
    assert coord.mode == "in"


def test_fade_out_completes_swaps_to_rotation_effect():
    """End-to-end: after the helper triggers the fade-out and the
    fade completes (driven by the `out` mode), the rotation effect
    takes over `self.current` and mode flips to `in`."""
    from lib_shared.patterns.media_cycler import MediaCycler
    from lib_shared.models import EffectsSettings

    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    mgr = _StubMessageManager(
        messages=[],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.05,
            hold_seconds=1.0,
            idle_seconds=1.0,
        ),
    )
    coord = _build_coord(message_manager=mgr)
    coord.effects = [fx_a, fx_b]
    coord.idx = 0
    coord.mode = "hold"

    cycler = MediaCycler(
        "m6",
        [{"type": "image/jpeg", "url": "key/f.jpg"}],
        display=coord.display,  # type: ignore[arg-type]
    )
    cycler.complete = True
    coord.current = cycler

    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    try:
        coord._maybe_fall_back_to_rotation()
        # Helper set mode to `out`, cycler still in place.
        assert coord.mode == "out"
        assert coord.current is cycler

        # Drive a tick past fade_seconds — the `out` mode's fade
        # completes and the rotation swap runs.
        clock.advance(0.06)
        coord.tick()
        # Cycler replaced by the next rotation effect, fade-in started.
        assert coord.current is fx_b
        assert coord.mode == "in"
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# Media-cycler fresh-id interrupt suppression (issue #38 follow-up)
# ---------------------------------------------------------------------------


def test_current_is_active_media_cycler_true_when_cycler_active(tmp_path):
    """`_current_is_active_media_cycler` returns True when
    `self.current` is a MediaCycler whose `complete` and `exhausted`
    flags are both False — the cycler is still playing its natural
    duration and should suppress fresh-id interrupts."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0

    # Pre-populate the cache so the cycler's `_cycle_advance` can
    # build the inner renderer without dropping the item (which
    # would flip `exhausted=True` before the assertion).
    cache_dir = tmp_path / "media-cache"
    cached = _prep_cache_for_jpeg(cache_dir, "key/supp-a.jpg")

    cycler = MediaCycler(
        "m-supp-1",
        [{"type": "image/jpeg", "url": "key/supp-a.jpg", "path": str(cached)}],
        display=coord.display,  # type: ignore[arg-type]
        cache_dir=str(cache_dir),
    )
    # Defaults: complete=False, exhausted=False.
    assert cycler.exhausted is False  # type: ignore[attr-defined]
    assert cycler.complete is False  # type: ignore[attr-defined]
    coord.current = cycler
    assert coord._current_is_active_media_cycler() is True


def test_current_is_active_media_cycler_false_when_done(tmp_path):
    """`_current_is_active_media_cycler` returns False when the
    cycler has flipped `complete=True` or `exhausted=True` — the
    coordinator should let the interrupt through so the next
    fade (or the existing `_maybe_fall_back_to_rotation`) handles
    the transition."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0

    # Pre-populate the cache so the cycler's `_cycle_advance` can
    # build the inner renderer without dropping the item — we want
    # to test the explicit flag flip, not the construction-time drop.
    cache_dir = tmp_path / "media-cache"
    cached_b = _prep_cache_for_jpeg(cache_dir, "key/supp-b.jpg")
    cached_c = _prep_cache_for_jpeg(cache_dir, "key/supp-c.jpg")

    cycler_done = MediaCycler(
        "m-supp-2",
        [{"type": "image/jpeg", "url": "key/supp-b.jpg", "path": str(cached_b)}],
        display=coord.display,  # type: ignore[arg-type]
        cache_dir=str(cache_dir),
    )
    cycler_done.complete = True
    coord.current = cycler_done
    assert coord._current_is_active_media_cycler() is False

    cycler_exhausted = MediaCycler(
        "m-supp-3",
        [{"type": "image/jpeg", "url": "key/supp-c.jpg", "path": str(cached_c)}],
        display=coord.display,  # type: ignore[arg-type]
        cache_dir=str(cache_dir),
    )
    cycler_exhausted.exhausted = True
    coord.current = cycler_exhausted
    assert coord._current_is_active_media_cycler() is False


def test_current_is_active_media_cycler_false_for_normal_effect():
    """When `self.current` is a normal Effect (not a cycler),
    the helper returns False — no exemption applies."""
    fx_a = _make_effect("A")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a]
    coord.idx = 0
    coord.current = fx_a
    assert coord._current_is_active_media_cycler() is False


# ---------------------------------------------------------------------------
# Out→in transition: end-to-end
# ---------------------------------------------------------------------------


def test_out_to_in_picks_up_cycler_for_mms_message(tmp_path):
    """End-to-end: an MMS message lands in the buffer, the
    background→out transition fires, the out→in swap assigns a
    MediaCycler to `self.current`."""
    from lib_shared.models import EffectsSettings, Message, MessageView

    msg = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="with photo",
            received_at="2026-07-09T00:00:00Z",
            media=[{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        ),
        suppressed=False,
    )
    cache_dir = tmp_path / "media-cache"
    # Pre-populate the cache so the cycler can build a real
    # ImageDisplay inner renderer.
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/a.jpg")

    mgr = _StubMessageManager(
        messages=[msg],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=0.1,
            idle_seconds=0.05,
        ),
    )
    coord = _build_coord(
        message_manager=mgr,
        media_api_base_url="http://test",
        media_cache_dir=str(cache_dir),
    )
    # Patch time.monotonic so the state machine advances.
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    try:
        # Drive ticks: intro → out → in (the cycler should appear as
        # `self.current` after the in transition).
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out fade finishes → in
        clock.advance(0.02)
        coord.tick()  # in fade finishes → hold
        # After in→hold, self.current is the cycler.
        from lib_shared.patterns.media_cycler import MediaCycler

        assert isinstance(coord.current, MediaCycler)
        # The cycler has 1 item and is not exhausted.
        # `isinstance` doesn't narrow through Pyright on the cycler's
        # extra attributes — silence the report.
        current = coord.current
        assert current is not None
        assert current.exhausted is False  # type: ignore[attr-defined]
        assert current.items_remaining == 1  # type: ignore[attr-defined]  # noqa: E501
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# Suppression log: transition-only (regression for 2026-07-10 per-tick spam)
# ---------------------------------------------------------------------------


def test_background_suppression_log_fires_once_per_fresh_id(tmp_path, caplog):
    """Regression for heroku flood: `Coordinator background: suppressing
    fresh-id interrupt while media cycler active` was firing every tick
    (~5ms cadence), spamming identical lines as long as the top message
    stayed unconsumed and the cycler was active.

    Now the log fires only on the TRANSITION between suppressed ids.
    First tick suppresses → log. Next 10 ticks of the same id → no log.
    New id arrives → log fires once.
    """
    import logging

    from lib_shared.models import EffectsSettings, Message, MessageView
    from lib_shared.patterns.media_cycler import MediaCycler

    msg_a = MessageView(
        message=Message(id="mA", sender="+15551234567", body="a", received_at="2026-07-09T00:00:00Z"),
        suppressed=False,
    )
    # idle_seconds=10**9 keeps the coordinator in `background` mode for
    # the whole test (otherwise the `idle_elapsed` branch triggers an
    # out→in transition on the first tick and we leave background
    # before the suppression gate can be exercised across ticks).
    mgr = _StubMessageManager(
        messages=[msg_a],
        effects_settings=EffectsSettings(idle_seconds=1e9),
    )
    coord = _build_coord(message_manager=mgr)

    # Install a real cycler (not a stub) so the exemption path runs.
    cache_dir = tmp_path / "media-cache"
    cached = _prep_cache_for_jpeg(cache_dir, "key/spam-a.jpg")
    cycler = MediaCycler(
        "mA",
        [{"type": "image/jpeg", "url": "key/spam-a.jpg", "path": str(cached)}],
        display=coord.display,  # type: ignore[arg-type]
        cache_dir=str(cache_dir),
    )
    coord.current = cycler
    coord.mode = "background"
    coord.phase_start = 0.0  # irrelevant — idle_seconds is huge

    caplog.set_level(logging.INFO, logger="heart")

    # First tick (the suppression should log).
    coord.tick()
    first_count = sum(1 for r in caplog.records if "suppressing fresh-id interrupt" in r.message)
    assert first_count == 1, (
        f"first tick should log the suppression once; got {first_count}: "
        f"{[r.message for r in caplog.records if 'suppressing' in r.message]}"
    )

    # Next 10 ticks of the same id — no NEW log lines.
    for _ in range(10):
        coord.tick()
    repeat_count = sum(1 for r in caplog.records if "suppressing fresh-id interrupt" in r.message)
    assert repeat_count == 1, (
        f"transition-only gate broken: 10 same-id ticks produced "
        f"{repeat_count} suppression logs (expected 1). "
        f"Records: {[r.message for r in caplog.records if 'suppressing' in r.message]}"
    )

    # A new unconsumed ID arrives — the next tick logs the suppression.
    msg_b = MessageView(
        message=Message(id="mB", sender="+15551234567", body="b", received_at="2026-07-09T00:01:00Z"),
        suppressed=False,
    )
    mgr._entries.insert(0, msg_b)
    coord.tick()
    new_count = sum(1 for r in caplog.records if "suppressing fresh-id interrupt" in r.message)
    assert new_count == 2, (
        f"newly-suppressed id should produce one more log; got {new_count}: "
        f"{[r.message for r in caplog.records if 'suppressing' in r.message]}"
    )


def test_hold_interrupt_by_new_mms_refreshes_picked_entry_for_out_to_in(tmp_path):
    """Bug guard (issue #26/#38): when an MMS arrives during `hold`,
    the fresh-id interrupt path must pull the new message's body AND
    media metadata BEFORE kicking the fade-out, so the subsequent
    out→in transition can swap in a `MediaCycler` for the new image.

    Background: prior to this fix, the hold→out fresh-id path called
    `_begin_out(now)` without re-pulling — `_last_display_message`
    and `_last_picked_entry` stayed at the previous message. The next
    out→in landed on the *old* body with *no* media-override, so a
    second MMS (after a first MMS cycler had cycled out) appeared as
    plain text on a rotation effect while the diagnostic log warned
    "picked message has N media item(s) but current effect is X".
    Background→out already pulls (line 1184); this test pins the
    matching behavior at hold→out (line 1081).
    """
    from lib_shared.models import EffectsSettings, Message, MessageView

    # First message: text-only SMS that quickly lands us in hold.
    msg1 = MessageView(
        message=Message(
            id="m1",
            sender="+15551234567",
            body="first",
            received_at="2026-01-02T00:00:00Z",
        ),
        suppressed=False,
    )
    cache_dir = tmp_path / "media-cache"
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/b.jpg")

    mgr = _StubMessageManager(
        messages=[msg1],
        effects_settings=EffectsSettings(
            intro_seconds=0.0,
            fade_seconds=0.01,
            hold_seconds=999.0,  # never elapses on its own
            idle_seconds=999.0,
        ),
    )
    coord = _build_coord(
        message_manager=mgr,
        media_api_base_url="http://test",
        media_cache_dir=str(cache_dir),
    )
    clock = _Clock()
    monkey = pytest.MonkeyPatch()
    monkey.setattr(time, "monotonic", clock)

    try:
        # Drive through intro → out → in → hold on the SMS.
        coord.start()
        clock.advance(0.001)
        coord.tick()  # intro → out
        clock.advance(0.02)
        coord.tick()  # out fade finishes → in
        clock.advance(0.02)
        coord.tick()  # in fade finishes → hold
        assert coord.mode == "hold"
        assert coord.last_shown_text == "first"

        # Second message — an MMS — arrives while we're in hold.
        msg2 = MessageView(
            message=Message(
                id="m2",
                sender="+15551234567",
                body="with photo",
                received_at="2026-07-09T00:00:00Z",
                media=[{"type": "image/jpeg", "url": "media/images/2026-07/b.jpg"}],
            ),
            suppressed=False,
        )
        mgr.add_message(msg2)
        clock.advance(0.01)
        coord.tick()
        # The hold→out interrupt should have fired.
        assert coord.mode == "out", "hold→out fresh-id interrupt did not fire; " f"mode={coord.mode!r}"

        # The pull must have refreshed both the cached body and the
        # picked entry — these are the two fields the out→in
        # transition reads when it builds the media-override cycler.
        assert coord._last_display_message == "with photo", (
            "hold→out fresh-id interrupt did not pull the new message body; "
            f"_last_display_message={coord._last_display_message!r}"
        )
        picked = coord._last_picked_entry
        assert picked is not None, "hold→out fresh-id interrupt did not set _last_picked_entry"
        assert picked.message.id == "m2", "hold→out pulled the wrong message: " f"got id={picked.message.id!r}"
        assert picked.message.media, "pulled entry has empty media list; cycler won't be built"
    finally:
        monkey.undo()
