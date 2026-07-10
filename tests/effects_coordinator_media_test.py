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
    _StubCanvas,
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
        display=display,
        scroller=scroller,
        effects=[fx_a],
        heart=heart,
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
    monkeypatch.setattr(
        "lib_shared.config_reader.get_config",
        lambda required_keys=None: cfg_stub,
    )
    try:
        import lib_shared.patterns.image_display as _image_mod
    except ImportError:
        _image_mod = None
    if _image_mod is not None:
        monkeypatch.setattr(_image_mod, "get_config", lambda required_keys=None: cfg_stub)


@pytest.fixture(autouse=True)
def _auto_patch_config(monkeypatch):
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


def test_maybe_build_media_cycler_uses_default_cache_dir_when_unset(tmp_path):
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
        assert "lindsay-50-media" in str(cycler._cache_dir)
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
    assert cycler.exhausted is False
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


def test_maybe_fall_back_to_rotation_swaps_when_cycler_exhausted():
    """A `MediaCycler` with `exhausted=True` is swapped back to
    `self.effects[self.idx]`. The rotation effect resumes for the
    remainder of the hold."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a, fx_b]
    coord.idx = 1

    # Build a cycler and mark it exhausted.
    cycler = MediaCycler(
        "m1",
        [{"type": "image/jpeg", "url": "key/a.jpg"}],
        display=coord.display,
    )
    # Force the cycler into the exhausted state (D12: every item dropped).
    cycler.exhausted = True
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # self.current swapped back to effects[1] = fx_b.
    assert coord.current is fx_b


def test_maybe_fall_back_to_rotation_swaps_when_cycler_complete():
    """A `MediaCycler` with `complete=True` is swapped back to
    `self.effects[self.idx]` — same swap path as `exhausted`. The
    cycler played through its content (1-item ran for `duration`
    seconds; multi-item cycled every attachment); the coordinator
    takes the rotation effect for the remainder of the hold / idle
    window instead of looping the same frame."""
    from lib_shared.patterns.media_cycler import MediaCycler

    fx_a = _make_effect("A")()
    fx_b = _make_effect("B")()
    coord = _build_coord(message_manager=_StubMessageManager(messages=[]))
    coord.effects = [fx_a, fx_b]
    coord.idx = 1

    cycler = MediaCycler(
        "m2",
        [{"type": "image/jpeg", "url": "key/b.jpg"}],
        display=coord.display,
    )
    # `complete` is set when the cycler decides it's done — we
    # flip it manually here so the test doesn't have to drive the
    # elapsed-time path.
    cycler.complete = True
    cycler.exhausted = False  # mutually independent; cover both shapes
    coord.current = cycler

    coord._maybe_fall_back_to_rotation()
    # Same outcome as exhausted — swap back to effects[1].
    assert coord.current is fx_b


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
