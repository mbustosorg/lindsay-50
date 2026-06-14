## Why

`heart-message-manager/preview_renderer.py` re-implements the Pi's `EffectCoordinator` from `heart-matrix-controller/main.py` — the entire fade state machine (`mode`/`idx`/`fade_start`/`last_step` brightness ramp, two-stage fade, effect cycling) is duplicated verbatim. The seven effect patterns and the shared `Bitmap` / `Palette` / `Effect` / `arrayblit` primitives are shared only via filesystem symlinks under `static/preview/heart-matrix-controller/`, so they live in Pi-specific directories even though they have no rgbmatrix dependency. This makes the browser preview a fragile mirror of the Pi code, makes it hard to keep the two in sync, and prevents `Hyperspace` (and any future pattern) from being available in the browser preview without touching multiple files in both subsystems.

## What Changes

- **New `lib_shared/effect_base.py`** — extract `Bitmap`, `Palette`, `Effect`, `arrayblit` from `heart-matrix-controller/rgb_display.py`. These primitives have no rgbmatrix dependency and are reused by both the Pi device and the browser preview.
- **New `lib_shared/patterns/`** — relocate all seven pattern files (`fireworks.py`, `flame.py`, `nightsky.py`, `honeycomb.py`, `hyperspace.py`, `png_display.py`, `video_display.py`) from `heart-matrix-controller/patterns/`. Update their imports to `from lib_shared.effect_base import …`. Remove the now-empty `heart-matrix-controller/patterns/` directory.
- **New `lib_shared/coordinator_base.py`** — `CoordinatorBase` class that owns the fade state machine (`mode`/`idx`/`fade_start`/`last_step` brightness ramp, two-stage fade, effect cycling). One hook for subclasses: `_composite(effect, scroller)`. Subclasses may override `request_message` for pre-checks (e.g. dedup).
- **Refactor `EffectCoordinator` (Pi)** in `heart-matrix-controller/main.py` to a small subclass that implements `_composite` as `self.display.render(effect, scroller)` (preserving `SwapOnVSync` pacing).
- **Refactor `PreviewCoordinator` (browser)** in `heart-message-manager/preview_renderer.py` to a `CoordinatorBase` subclass. Keep the browser-specific `_last_text` dedup, the two read-only properties (`current_effect_name`, `current_text`), and the explicit `clear + effect.render + scroller.render` composite.
- **Update browser symlinks + `heart-message-manager/py-config.toml`** — add `lib_shared/effect_base.py` and the five browser-eligible `lib_shared/patterns/*` entries; remove the old `heart-matrix-controller/patterns/*` and `heart-matrix-controller/rgb_display.py` entries. Update `preview_main.py` imports to `from lib_shared.patterns import …` and `import lib_shared.patterns as patterns`.
- **Remove dead `config_reader.py` browser symlink** — the browser never imports it. Keep the server-side module (used by `main.py`, `auth.py`, `s3.py`).
- **Wire `Hyperspace` into the browser preview** — currently skipped in `_BROWSER_COMPATIBLE_PATTERNS`, `py-config.toml`, and the `preview_main.py` import line. It has no extra dependencies beyond `lib_shared.effect_base` (its `tick()` uses `arrayblit` and the abstract canvas, both already shared), so this is pure wire-up, not a code change:
  - Add `"Hyperspace"` to `_BROWSER_COMPATIBLE_PATTERNS` in `preview_renderer.py`.
  - Add `from lib_shared.patterns import …, hyperspace` in `preview_main.py`.
  - Add `lib_shared/patterns/hyperspace.py` to `py-config.toml` `[files]`.
  - Add a browser symlink at `heart-message-manager/static/preview/lib_shared/patterns/hyperspace.py` → `lib_shared/patterns/hyperspace.py`.
- **Add `tests/coordinator_base_test.py`** — minimal state-machine coverage: `idle → out → in → idle` transitions, `idx` advance on fade-out complete, brightness ramp endpoints, `pending_text` behavior.

## Capabilities

### New Capabilities

- `shared-effect-rendering`: the shared effect primitives (`Bitmap`, `Palette`, `Effect`, `arrayblit`), the `CoordinatorBase` fade state machine, and the relocated `lib_shared/patterns/` collection — used by both the Pi device's `EffectCoordinator` and the browser preview's `PreviewCoordinator`.

### Modified Capabilities

_None — no existing spec-level requirements change._

## Impact

- **New files**: `lib_shared/effect_base.py`, `lib_shared/coordinator_base.py`, `lib_shared/patterns/{__init__,fireworks,flame,nightsky,honeycomb,hyperspace,png_display,video_display}.py`, `tests/coordinator_base_test.py`.
- **Modified files**: `heart-matrix-controller/rgb_display.py` (split out primitives), `heart-matrix-controller/main.py` (subclass `CoordinatorBase`), `heart-message-manager/preview_renderer.py` (subclass `CoordinatorBase`), `heart-message-manager/preview_main.py` (update imports), `heart-message-manager/py-config.toml` (new file entries, drop old ones).
- **Symlinks added** under `heart-message-manager/static/preview/lib_shared/`: 5 patterns + `effect_base.py`.
- **Symlinks removed** under `heart-message-manager/static/preview/heart-matrix-controller/`: 5 patterns + `rgb_display.py` + dead `config_reader.py`.
- **Removed directory**: `heart-matrix-controller/patterns/`.
- **Out of scope** (separate work, not consolidation): PngDisplay in the browser preview, VideoDisplay in the browser preview, replacing 3s polling with WebSocket, any new patterns.
