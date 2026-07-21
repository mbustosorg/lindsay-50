"""Tests for `lib_shared.patterns.media_cycler.MediaCycler` (issue #38).

`MediaCycler` is the per-message background effect constructed by the
`EffectsCoordinator` at the `out → in` transition when the inbound
message has a non-empty `media` list. This test file pins the
contract from the openspec (`add-image-and-video-support`):

- mime-type dispatch: image/* → ImageDisplay, video/* → VideoDisplay
  (D7 — direct imports, no `effects_loader` lookup)
- codec-failure handling (D12): bad items get dropped, list-empty
  → `exhausted = True` so the coordinator falls back to the rotation
- 1-item media: never `exhausted` (coordinator handles cutoff via
  `hold_seconds`)
- multi-item: uniform random from not-yet-shown-this-cycle items
- per-item duration read for video (`cv2.CAP_PROP_FRAME_COUNT` /
  `cv2.CAP_PROP_FPS`) cached at construction
- `set_brightness` / `tick` / `render` forward to the active inner
  renderer
- inner renderers NOT in the effects registry (D6)

Tests use a `fetcher` callable + pre-populated local cache, so the
cycler never hits the network. Inner renderers are constructed
against stub displays with `_StubCanvas` so we don't need Pillow's
image loader.
"""

from __future__ import annotations

import hashlib
import io
import logging
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubCanvas:
    """Minimal canvas stub — `width` / `height` and a no-op SetPixel."""

    width = 8
    height = 8

    def SetPixel(self, *args, **kwargs):
        pass

    def SetImage(self, *args, **kwargs):
        pass


class _StubDisplay:
    """Display stub matching the surface MediaCycler reads."""

    def __init__(self, w: int = 8, h: int = 8):
        self.width = w
        self.height = h
        self.canvas = _StubCanvas()


def _make_image_bytes() -> bytes:
    """Return real PNG bytes for ImageDisplay's `_render_image` path."""
    from PIL import Image

    img = Image.new("RGB", (8, 8), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_image(path: Path) -> None:
    """Write a tiny valid PNG to `path` so ImageDisplay can open it."""
    path.write_bytes(_make_image_bytes())


def _cache_path_for(cache_dir: Path, key: str, ext: str) -> Path:
    """Predict the cache filename the cycler uses for `key`."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{digest}{ext}"


def _prep_cache_for_jpeg(cache_dir: Path, key: str) -> Path:
    """Write a real PNG to the cache file the cycler will look up
    for an `image/jpeg` item with `key`. Returns the cache path."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _cache_path_for(cache_dir, key, ".jpg")
    _write_image(p)
    return p


@pytest.fixture
def cfg_stub(monkeypatch):
    """Patch `lib_shared.config_reader.get_config` so ImageDisplay's
    `cfg.if_exists(...)` calls return safe defaults during host tests.

    The image_display module imports `get_config` at module load
    via `from lib_shared.config_reader import get_config`, so the
    name is bound in image_display's globals at import time. We
    patch BOTH the source module attribute and the consumer
    module's globals to be safe.
    """
    stub = SimpleNamespace(
        if_exists=lambda k: {"PNG_INTERVAL": "0.05", "PNG_FADE": "0.02"}.get(k),
    )
    monkeypatch.setattr(
        "lib_shared.config_reader.get_config",
        lambda required_keys=None: stub,
    )
    # Also patch the name in image_display's globals if it has been
    # imported by an earlier test (or by this fixture's caller).
    try:
        import lib_shared.patterns.image_display as _image_mod
    except ImportError:
        _image_mod = None
    if _image_mod is not None:
        monkeypatch.setattr(_image_mod, "get_config", lambda required_keys=None: stub)
    return stub


def _make_cycler(message_id: str, media: list, *, cache_dir: Path, **kwargs):
    """Build a cycler with a `fetcher` that returns bytes from a
    pre-populated cache directory. The cycler will find the cached
    file via the sha256 path and never call the fetcher."""
    kwargs.setdefault("api_base_url", "http://test")
    kwargs.setdefault("hold_seconds", 1e9)  # never cut off in tests
    kwargs.setdefault("display", _StubDisplay())
    return _mod().MediaCycler(
        message_id,
        media,
        cache_dir=cache_dir,
        **kwargs,
    )


def _mod():
    """Lazy import so the test module can be collected without the
    `cfg_stub` patch being active at import time."""
    import importlib

    return importlib.import_module("lib_shared.patterns.media_cycler")


# ---------------------------------------------------------------------------
# Construction / mime dispatch
# ---------------------------------------------------------------------------


def test_media_cycler_module_importable():
    """`MediaCycler` is importable from `lib_shared.patterns.media_cycler`."""
    mod = _mod()
    assert hasattr(mod, "MediaCycler")
    assert callable(mod.MediaCycler)


def test_media_cycler_imports_inner_renderers_directly():
    """Direct-imports check (D7): the cycler references `image_display`
    and `video_display` modules by direct import, not via the
    effects loader."""
    cycler_mod = _mod()
    import lib_shared.patterns.image_display as image_mod
    import lib_shared.patterns.video_display as video_mod

    assert hasattr(image_mod, "ImageDisplay")
    assert hasattr(video_mod, "VideoDisplay")
    cycler_src = Path(str(cycler_mod.__file__)).read_text()
    assert "image_display" in cycler_src
    assert "video_display" in cycler_src


def test_media_cycler_inner_renderers_not_in_registry():
    """D6: ImageDisplay / VideoDisplay are NOT entries in the canonical
    effects list. Pin that operator overrides carrying these names
    land gracefully (handled by `make_effect_class` returning None)."""
    from lib_shared.effects_loader import load_effects_settings, make_effect_class

    load_effects_settings()
    canonical_names = {e["name"] for e in load_effects_settings()["effects"]}
    assert "ImageDisplay" not in canonical_names
    assert "VideoDisplay" not in canonical_names
    assert make_effect_class("ImageDisplay") is None
    assert make_effect_class("VideoDisplay") is None


def test_media_cycler_empty_media_signals_exhausted():
    """An empty `media` list signals `exhausted = True` at construction
    so the coordinator falls back to the rotation immediately."""
    cycler = _make_cycler("msg-1", [], cache_dir=Path("/tmp"))
    assert cycler.exhausted is True
    assert cycler.items_remaining == 0
    assert cycler.active_url == ""


def test_media_cycler_drops_malformed_entries(cfg_stub, tmp_path):
    """Entries without `type` or `url` are dropped at construction
    with a WARNING — a malformed entry must not crash the cycle."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    media = [
        {"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"},
        {"type": "", "url": "media/images/2026-07/b.jpg"},  # no mime
        {"type": "image/jpeg", "url": ""},  # no key
        {"type": "image/jpeg", "url": "media/images/2026-07/c.jpg"},
    ]
    # Pre-populate cache for the well-formed entries.
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/a.jpg")
    _prep_cache_for_jpeg(cache_dir, "media/images/2026-07/c.jpg")
    cycler = _make_cycler("msg-2", media, cache_dir=cache_dir)
    # Only the two well-formed entries are kept.
    assert cycler.items_remaining == 2


# ---------------------------------------------------------------------------
# Effect interface forwarding
# ---------------------------------------------------------------------------


def test_media_cycler_image_item_uses_image_display(tmp_path, cfg_stub):
    """An `image/jpeg` item constructs an `ImageDisplay` inner renderer."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-3",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
    )
    assert cycler._active is not None
    assert type(cycler._active).__name__ == "ImageDisplay"


def test_media_cycler_completes_at_hold_cutoff_when_item_outlives_hold(tmp_path, cfg_stub):
    """Regression: an item whose natural window outlives `hold_seconds`
    must still flip `complete` at the hold cutoff.

    Without this, the coordinator's hold clock cuts the message off but
    the cycler never reports done, so the next out→in rebuilds this same
    cycler and the media loops forever instead of yielding to the
    rotation effect. An image's natural window is `_MIN_ITEM_SECONDS`
    (10s); with `hold_seconds=4` the per-item logic wouldn't flip
    `complete` until 10s, but the whole-cycler cutoff flips it at ~4s.
    Time is simulated by shifting the cycler's internal clocks back so
    the test never sleeps.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-cutoff",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
        hold_seconds=4.0,
    )
    assert cycler.complete is False
    # 3s elapsed — before the 4s cutoff and before the 10s natural window.
    cycler._started -= 3.0
    cycler._phase_start -= 3.0
    cycler.tick()
    assert cycler.complete is False, "flipped complete before the hold cutoff"
    # 5s elapsed — past the 4s hold cutoff (still short of the 10s natural
    # window), so ONLY the whole-cycler backstop can flip complete here.
    cycler._started -= 2.0
    cycler.tick()
    assert cycler.complete is True


def test_media_cycler_video_item_uses_video_display(tmp_path):
    """A `video/mp4` item constructs a `VideoDisplay` inner renderer.

    VideoDisplay gracefully accepts a missing file at construction
    (logs WARNING, `_cap=None`) — but the class identity still holds.
    We pre-populate the cache with empty bytes, and a fetcher that
    returns non-empty bytes — so the cycler fetches, writes a real
    file to the cache, then VideoDisplay fails to open (cv2 missing
    on host), `_cap=None`, but the class identity holds.
    """
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/videos/2026-07/a.mp4"

    def fetcher(url):
        return b"fake video bytes"

    cycler = _make_cycler(
        "msg-4",
        [{"type": "video/mp4", "url": key}],
        cache_dir=cache_dir,
        fetcher=fetcher,
    )
    assert cycler._active is not None
    assert type(cycler._active).__name__ == "VideoDisplay"


def test_media_cycler_set_brightness_forwards(tmp_path, cfg_stub):
    """`set_brightness(b)` forwards to the active inner renderer,
    multiplied by the brightness boost (default 1.15x). The cycler
    keeps `self._brightness` as the unboosted value (the
    coordinator's b) and pushes `b * boost` to the inner renderer
    so the on-panel brightness is the boosted product."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-5",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
    )
    assert cycler._active is not None
    cycler.set_brightness(0.5)
    # Cycler's introspection field stays at the unboosted value —
    # the coordinator asks for 0.5 brightness, the cycler reports 0.5.
    assert cycler._brightness == 0.5
    # And the inner ImageDisplay gets the boosted product so the
    # palette pushes the image ~15% brighter at full panel brightness.
    inner = cycler._active
    assert getattr(inner, "_coord_b", None) == 0.5 * 1.15 or getattr(inner, "_brightness", None) == 0.5 * 1.15


def test_media_cycler_set_brightness_boost_is_configurable(tmp_path, cfg_stub):
    """The `brightness_boost` constructor kwarg overrides the
    module-level default — useful for tests that want to assert the
    exact forwarded brightness without the 1.15x multiplier."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-5b",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
        brightness_boost=1.0,  # disable the boost
    )
    assert cycler._active is not None
    cycler.set_brightness(0.5)
    # With boost=1.0 the inner renderer sees the unboosted value.
    inner = cycler._active
    assert getattr(inner, "_coord_b", None) == 0.5 or getattr(inner, "_brightness", None) == 0.5


def test_media_cycler_tick_renders_and_forwards(tmp_path, cfg_stub):
    """`tick()` and `render()` forward to the active renderer. The
    stub canvas records pixel writes — at least one SetPixel call
    proves the inner renderer ran."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)

    canvas_pixels: list = []

    class _RecCanvas:
        width = 8
        height = 8

        def SetPixel(self, x, y, r, g, b):
            canvas_pixels.append((x, y, r, g, b))

        def SetImage(self, *a, **kw):
            pass

    class _RecDisplay:
        width = 8
        height = 8

        def __init__(self):
            self.canvas = _RecCanvas()

    cycler = _make_cycler(
        "msg-6",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
        display=_RecDisplay(),
    )
    cycler.tick()
    cycler.render(cycler._display.canvas)
    # At least one pixel write (the image is non-empty).
    assert canvas_pixels, "render did not forward to the inner ImageDisplay"


# ---------------------------------------------------------------------------
# Codec-failure handling (D12)
# ---------------------------------------------------------------------------


def test_media_cycler_drops_codec_failure_on_construction(tmp_path):
    """A media entry whose inner renderer raises during construction
    is dropped from `self._items` — no crash, no black panel."""
    # Hand the cycler a path that will make ImageDisplay raise
    # (corrupt bytes that PIL cannot identify).
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _cache_path_for(cache_dir, key, ".jpg").write_bytes(b"NOT A REAL JPEG FILE")

    def fetcher(url):
        return b"NOT A REAL JPEG FILE"

    cycler = _make_cycler(
        "msg-7",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
        fetcher=fetcher,
    )
    # The bad item was dropped; the list is empty; exhausted is True.
    assert cycler.items_remaining == 0
    assert cycler.exhausted is True
    assert cycler._active is None


def test_media_cycler_codec_failure_during_tick_drops_item(tmp_path, cfg_stub):
    """A codec failure on `tick()` drops the offending item (D12).

    We inject a failing renderer to simulate a runtime codec failure.
    The cycler catches the exception, drops the item, and flips
    `exhausted` to True when the list becomes empty."""

    class _BoomRenderer:
        """Inner renderer that raises on `tick` (codec failure simulator)."""

        def __init__(self):
            self.tick_calls = 0
            self.render_calls = 0

        def set_brightness(self, b):
            pass

        def tick(self):
            self.tick_calls += 1
            raise OSError("simulated codec failure")

        def render(self, canvas):
            self.render_calls += 1

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    # Give the item a real cache path so it doesn't drop on the
    # codec-failure path during _build_inner (PIL would actually
    # parse this PNG, so the inner build succeeds and tick is what
    # raises). We patch the renderer AFTER construction to inject
    # the boom behavior.
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-8",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
    )
    # Replace the active renderer with the boom one and link it
    # to the item so `_drop_active` can find it.
    boom = _BoomRenderer()
    cycler._items[0]["renderer"] = boom
    cycler._active = boom

    cycler.tick()  # BoomRenderer.tick raises → drop
    # The item was dropped, the list is empty, exhausted is True.
    assert cycler.items_remaining == 0
    assert cycler.exhausted is True


def test_media_cycler_one_item_does_not_signal_exhausted(tmp_path, cfg_stub):
    """1-item media never signals `exhausted` — the coordinator
    handles the cutoff via `hold_seconds` (D5: cycler's "advance or
    hold" clock is gated by hold_seconds, not by an internal counter)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-9",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
    )
    assert cycler.exhausted is False
    assert cycler.items_remaining == 1
    # Many ticks later — still not exhausted (1-item never advances internally).
    for _ in range(50):
        cycler.tick()
    assert cycler.exhausted is False
    assert cycler.items_remaining == 1


# ---------------------------------------------------------------------------
# `complete` flag — the cycler signals "I've shown everything I was given"
# so the coordinator can swap us out for the rotation effect instead of
# looping the same frame for `idle_seconds`.
# ---------------------------------------------------------------------------


def test_media_cycler_one_item_complete_flips_after_duration(tmp_path, cfg_stub):
    """1-item cycler flips `complete = True` once `item["duration"]`
    seconds have elapsed in the active phase. The coordinator reads
    this on the next tick and swaps the cycler out for the rotation
    effect — prevents the user-visible "frozen last frame sits for
    60s" symptom from empty-body MMS in background mode."""

    class _FakeRenderer:
        def set_brightness(self, b):
            pass

        def tick(self):
            pass

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/one.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler("msg-c1", [{"type": "image/jpeg", "url": key}], cache_dir=cache_dir)
    # The cycler already built its inner renderer (ImageDisplay). We
    # swap in a FakeRenderer so we don't need to drive ImageDisplay's
    # state machine — only the cycler's elapsed-vs-duration check.
    fake = _FakeRenderer()
    cycler._items[0]["renderer"] = fake
    cycler._active = fake
    cycler._phase = "hold"
    cycler._phase_start = time.monotonic()
    cycler._items[0]["duration"] = 5.0  # arbitrary

    assert cycler.complete is False
    # Tick with elapsed=0 → still not complete.
    cycler.tick()
    assert cycler.complete is False
    # Force elapsed > duration.
    cycler._phase_start = time.monotonic() - 6.0
    cycler.tick()
    assert cycler.complete is True
    # Stays True on subsequent ticks (one-shot flip).
    cycler.tick()
    assert cycler.complete is True


def test_media_cycler_one_item_complete_stays_false_under_duration(tmp_path, cfg_stub):
    """1-item cycler does NOT flip `complete` while elapsed < duration.
    The coordinator uses this to keep the cycler in place for the
    item's natural display window."""

    class _FakeRenderer:
        def set_brightness(self, b):
            pass

        def tick(self):
            pass

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/two.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler("msg-c2", [{"type": "image/jpeg", "url": key}], cache_dir=cache_dir)
    fake = _FakeRenderer()
    cycler._items[0]["renderer"] = fake
    cycler._active = fake
    cycler._phase = "hold"
    cycler._phase_start = time.monotonic()
    cycler._items[0]["duration"] = 30.0  # bigger than test runtime

    # Many ticks with elapsed=0 — complete stays False.
    for _ in range(20):
        cycler.tick()
    assert cycler.complete is False


def test_media_cycler_multi_item_complete_flips_when_all_shown(tmp_path, cfg_stub):
    """Multi-item cycler flips `complete = True` when every item has
    been shown at least once. After that, the coordinator swaps us
    out for the rotation effect — no looping forever."""

    class _FakeRenderer:
        def __init__(self):
            self.idx = -1

        def set_brightness(self, b):
            pass

        def tick(self):
            pass

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    images = []
    for i in range(3):
        key = f"media/images/2026-07/c3-{i}.jpg"
        _prep_cache_for_jpeg(cache_dir, key)
        images.append({"type": "image/jpeg", "url": key})

    cycler = _make_cycler("msg-c3", images, cache_dir=cache_dir)
    # Replace renderers with fakes (deterministic) and pre-mark all
    # items as shown so the next `_cycle_advance()` lands on the
    # "all_shown" branch and flips `complete`.
    fakes = [_FakeRenderer() for _ in range(3)]
    for item, fake in zip(cycler._items, fakes):
        item["renderer"] = fake
        item["shown"] = True
    cycler._active = fakes[0]
    cycler._phase = "hold"

    assert cycler.complete is False
    cycler._cycle_advance()  # picks from self._items (all shown → complete=True)
    assert cycler.complete is True


def test_media_cycler_multi_item_complete_stays_false_while_some_unshown(tmp_path, cfg_stub):
    """Multi-item cycler does NOT flip `complete` while some items
    are still unshown — keeps the rotation going through the full
    attachment list before signaling done."""

    class _FakeRenderer:
        def set_brightness(self, b):
            pass

        def tick(self):
            pass

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    images = []
    for i in range(3):
        key = f"media/images/2026-07/c4-{i}.jpg"
        _prep_cache_for_jpeg(cache_dir, key)
        images.append({"type": "image/jpeg", "url": key})

    cycler = _make_cycler("msg-c4", images, cache_dir=cache_dir)
    fakes = [_FakeRenderer() for _ in range(3)]
    for item, fake in zip(cycler._items, fakes):
        item["renderer"] = fake
    # Two of three shown; one unshown.
    cycler._items[0]["shown"] = True
    cycler._items[1]["shown"] = True
    cycler._items[2]["shown"] = False
    cycler._active = fakes[0]
    cycler._phase = "hold"

    cycler._cycle_advance()  # picks an unshown item; not all shown
    assert cycler.complete is False


# ---------------------------------------------------------------------------
# Multi-item cycling
# ---------------------------------------------------------------------------


def test_media_cycler_multi_item_eventually_shows_all_items(tmp_path, cfg_stub):
    """Multi-item cycling marks each item as `shown=True` after enough
    cycles. With `random.choice` over not-yet-shown items, every
    item gets picked at least once over N cycles."""
    import random as _random

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    images = []
    for i in range(3):
        key = f"media/images/2026-07/img-{i}.jpg"
        _prep_cache_for_jpeg(cache_dir, key)
        images.append({"type": "image/jpeg", "url": key})

    cycler = _make_cycler(
        "msg-10",
        images,
        cache_dir=cache_dir,
    )
    assert cycler.items_remaining == 3
    # Drive enough cycles that every item is picked at least once.
    for _ in range(20):
        cycler.tick()
        # Force advance by simulating elapsed time.
        cycler._phase_start = time.monotonic() - 100.0
        cycler.tick()
    shown = [it for it in cycler._items if it["shown"]]
    # All 3 items have been shown at least once.
    assert len(shown) == 3


def test_media_cycler_cycle_cuts_off_at_hold_seconds():
    """When the cumulative display time exceeds `hold_seconds`, the
    cycler does NOT advance further. The coordinator handles the
    actual hold → text_out transition."""

    class _FakeRenderer:
        def __init__(self):
            self.tick_calls = 0

        def set_brightness(self, b):
            pass

        def tick(self):
            self.tick_calls += 1

    cycler = _mod().MediaCycler(
        "msg-11",
        [{"type": "image/jpeg", "url": f"media/images/2026-07/img-{i}.jpg"} for i in range(3)],
        display=_StubDisplay(),
        hold_seconds=0.1,
    )
    # Manually inject items + renderers with known durations.
    cycler._items = [
        {
            "type": "image/jpeg",
            "url": f"media/images/2026-07/img-{i}.jpg",
            "shown": False,
            "path": "/nonexistent.jpg",
            "duration": 10.0,
            "renderer": _FakeRenderer(),
        }
        for i in range(3)
    ]
    cycler._active = cycler._items[0]["renderer"]
    cycler._phase = "hold"
    cycler._phase_start = time.monotonic() - 1.0  # elapsed=1.0 > hold=0.1

    cycler.tick()  # elapsed >= duration → would advance; but elapsed >= hold_seconds → hold
    # Active item unchanged.
    assert cycler._active is cycler._items[0]["renderer"]


# ---------------------------------------------------------------------------
# Per-item duration read for video (cached)
# ---------------------------------------------------------------------------


def test_media_cycler_video_duration_is_cached(tmp_path):
    """Video item's natural duration is read at construction and
    cached on the item — subsequent advance cycles reuse it."""

    class _FakeCap:
        def __init__(self, fps, frame_count):
            self._fps = fps
            self._frame_count = frame_count
            self.get_calls = 0
            self.release_calls = 0

        def isOpened(self):
            return True

        def get(self, prop):
            self.get_calls += 1
            # cv2.CAP_PROP_FPS = 5, cv2.CAP_PROP_FRAME_COUNT = 7
            if prop == 5:
                return float(self._fps)
            if prop == 7:
                return float(self._frame_count)
            return 0.0

        def release(self):
            self.release_calls += 1

    # The cycler calls cv2.VideoCapture(path). We patch via sys.modules
    # so the cycler's lazy `import cv2` inside _read_duration returns
    # our shim.
    import types

    fake_cap = _FakeCap(fps=30.0, frame_count=300)  # 10-second video
    fake_cv2 = types.ModuleType("cv2")
    setattr(fake_cv2, "VideoCapture", lambda path: fake_cap)
    setattr(fake_cv2, "CAP_PROP_FPS", 5)
    setattr(fake_cv2, "CAP_PROP_FRAME_COUNT", 7)
    sys.modules["cv2"] = fake_cv2

    try:
        key = "media/videos/2026-07/v.mp4"
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        cached = _cache_path_for(cache_dir, key, ".mp4")
        cached.write_bytes(b"fake video bytes")

        cycler = _mod().MediaCycler(
            "msg-12",
            [{"type": "video/mp4", "url": key}],
            display=_StubDisplay(),
            cache_dir=cache_dir,
        )
        item = cycler._items[0]
        assert item["duration"] >= 10.0
        assert fake_cap.get_calls >= 2
        assert fake_cap.release_calls >= 1
    finally:
        sys.modules.pop("cv2", None)


def test_media_cycler_image_item_duration_uses_floor(tmp_path, cfg_stub):
    """Image items don't have a natural duration; the cycler uses
    the `_MIN_ITEM_SECONDS` floor (10.0)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    _prep_cache_for_jpeg(cache_dir, key)
    cycler = _make_cycler(
        "msg-13",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
    )
    item = cycler._items[0]
    assert item["duration"] == 10.0  # the floor


# ---------------------------------------------------------------------------
# Fetch failure / cache write failure (D12 boundary)
# ---------------------------------------------------------------------------


def test_media_cycler_fetch_failure_drops_item(tmp_path):
    """When the fetcher raises (e.g., S3 outage), the item is dropped
    from `self._items` — D12: codec-failure semantics (we never got
    to attempt decode, but the observable effect is identical)."""

    def failing_fetcher(url):
        raise RuntimeError("simulated S3 outage")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cycler = _make_cycler(
        "msg-14",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        cache_dir=cache_dir,
        fetcher=failing_fetcher,
    )
    assert cycler.items_remaining == 0
    assert cycler.exhausted is True


def test_media_cycler_empty_response_body_drops_item(tmp_path):
    """When the fetcher returns empty bytes, the item is dropped."""

    def empty_fetcher(url):
        return b""

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cycler = _make_cycler(
        "msg-15",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        cache_dir=cache_dir,
        fetcher=empty_fetcher,
    )
    assert cycler.items_remaining == 0
    assert cycler.exhausted is True


def test_media_cycler_no_api_base_url_drops_items(tmp_path):
    """When `api_base_url` is empty, the cycler cannot fetch. Items
    are dropped, `exhausted` flips on. This is the path a
    misconfigured device takes (no Flask URL)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cycler = _make_cycler(
        "msg-16",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        cache_dir=cache_dir,
        api_base_url="",
    )
    assert cycler.items_remaining == 0
    assert cycler.exhausted is True


# ---------------------------------------------------------------------------
# Fetching is cached across cycles
# ---------------------------------------------------------------------------


def test_media_cycler_caches_across_cycles(tmp_path, cfg_stub):
    """The same S3 key is fetched at most ONCE per MediaCycler
    lifetime — re-entry into the cycle reuses the cached path."""

    fetch_count = {"n": 0}

    def counting_fetcher(url):
        fetch_count["n"] += 1
        return _make_image_bytes()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    key = "media/images/2026-07/a.jpg"
    cycler = _make_cycler(
        "msg-17",
        [{"type": "image/jpeg", "url": key}],
        cache_dir=cache_dir,
        fetcher=counting_fetcher,
    )
    initial_calls = fetch_count["n"]
    # The first cycle fetched the item. Subsequent ticks should NOT
    # refetch (the cache file already exists).
    for _ in range(20):
        cycler.tick()
    # Same call count — no extra fetches.
    assert fetch_count["n"] == initial_calls


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


def test_media_cycler_builds_proxy_url(tmp_path, cfg_stub):
    """The cycler builds `{api_base_url}/api/media/{key}` URLs."""

    fetch_calls: list[str] = []

    def recording_fetcher(url):
        fetch_calls.append(url)
        return _make_image_bytes()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cycler = _make_cycler(
        "msg-18",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        cache_dir=cache_dir,
        fetcher=recording_fetcher,
    )
    # The fetcher was called once with the proxy URL.
    assert len(fetch_calls) == 1
    assert fetch_calls[0] == "http://test/api/media/media/images/2026-07/a.jpg"


def test_media_cycler_trailing_slash_in_api_base_url_normalized(tmp_path, cfg_stub):
    """A trailing `/` on `api_base_url` is normalized — no double-slash."""

    fetch_calls: list[str] = []

    def recording_fetcher(url):
        fetch_calls.append(url)
        return _make_image_bytes()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    _make_cycler(
        "msg-19",
        [{"type": "image/jpeg", "url": "media/images/2026-07/a.jpg"}],
        cache_dir=cache_dir,
        api_base_url="http://test/",
        fetcher=recording_fetcher,
    )
    assert fetch_calls[0] == "http://test/api/media/media/images/2026-07/a.jpg"
    assert "//api" not in fetch_calls[0]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_media_cycler_logs_warning_on_drop(caplog, tmp_path):
    """A dropped item logs a WARNING that includes the S3 key (D12
    observability contract — operators see which attachment failed)."""

    def failing_fetcher(url):
        raise RuntimeError("simulated S3 outage")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    with caplog.at_level(logging.WARNING, logger="heart"):
        _make_cycler(
            "msg-20",
            [{"type": "image/jpeg", "url": "media/images/2026-07/bad.jpg"}],
            cache_dir=cache_dir,
            fetcher=failing_fetcher,
        )

    # The fetch failure logs a WARNING with the key.
    assert any(
        r.levelno == logging.WARNING and "bad.jpg" in r.getMessage() for r in caplog.records
    ), f"expected WARNING mentioning bad.jpg in records; got {[r.getMessage() for r in caplog.records]}"
