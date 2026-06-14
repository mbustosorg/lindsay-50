## 1. Extract shared effect primitives

- [ ] 1.1 Create `lib_shared/effect_base.py` with `Bitmap`, `Palette`, `arrayblit`, and the `Effect` base class, moved verbatim from `heart-matrix-controller/rgb_display.py`
- [ ] 1.2 Add `tests/effect_base_test.py` covering: `Bitmap` set/get/fill, `Palette` set/get/len, `arrayblit` happy path + wrong-size raise, `Effect.set_brightness` scales each color channel by the brightness factor
- [ ] 1.3 Strip the moved primitives from `heart-matrix-controller/rgb_display.py` (no re-export shim — that file becomes a thin wrapper around `Display`)

## 2. Rename Pi Display and its file

- [ ] 2.1 `git mv` `heart-matrix-controller/rgb_display.py` → `rgb_matrix_display.py`
- [ ] 2.2 Rename the class `Display` → `MatrixDisplay` inside the new file
- [ ] 2.3 Update `heart-matrix-controller/main.py` import to `from rgb_matrix_display import MatrixDisplay` and update the local binding (`display = MatrixDisplay()`)

## 3. Move patterns to lib_shared

- [ ] 3.1 `git mv` all eight pattern files (`fireworks.py`, `flame.py`, `nightsky.py`, `honeycomb.py`, `hyperspace.py`, `png_display.py`, `video_display.py`, `heartbeat.py`) from `heart-matrix-controller/patterns/` to `lib_shared/patterns/`
- [ ] 3.2 In each moved pattern file, replace `from rgb_display import …` with `from lib_shared.effect_base import …` for the four primitives only (keep the `Display` import out — patterns don't need it)
- [ ] 3.3 Delete the now-empty `heart-matrix-controller/patterns/` directory
- [ ] 3.4 Add `tests/patterns_import_test.py` that imports each of the eight pattern modules from `lib_shared.patterns` and constructs each pattern against a stub `display` (covers happy path for the import + construction surface)

## 4. Create DisplayBase

- [ ] 4.1 Create `lib_shared/display_base.py` with `DisplayBase` declaring one abstract method `render(effect, scroller)` and the concrete surface (`clear()`, `width`, `height`, `canvas`) the effects and coordinator rely on
- [ ] 4.2 Make `heart-matrix-controller/rgb_matrix_display.py:MatrixDisplay` subclass `DisplayBase`; `MatrixDisplay.render` keeps its current behavior (clear → effect.render → scroller.render → SwapOnVSync)
- [ ] 4.3 Add `tests/display_base_test.py` covering: `DisplayBase` raises on direct instantiation, the Pi's `MatrixDisplay` is a `DisplayBase` subclass, and `MatrixDisplay.render` invokes `SwapOnVSync` once per call (using a stub matrix that records calls)

## 5. Create shared EffectsCoordinator

- [ ] 5.1 Create `lib_shared/effects_coordinator.py` with concrete `EffectsCoordinator` (the merged state machine — `intro` → `out` → `in` → `hold` → `text_out` → `background`); `__init__` takes `display`, `scroller`, `effects`, `heart`, and optional `recent_provider` (default `None` → use internal `_recent` deque); `request_message` ignores empty text and dedupes the internal deque against the most recent entry; `start`, `tick`, `current_effect_name`, `current_text` work as the merged behavior; `tick()` ends with `self.display.render(self.current, self.scroller)`
- [ ] 5.2 Add `tests/effects_coordinator_test.py` covering: `intro → out → in → background` lifecycle, `idx` advance on fade-out complete, brightness-ramp endpoints (set_brightness(0) at progress=0, set_brightness(1) at progress=1), `pending_text` consume-on-transition, `hold`-mode interrupt by a new message, `request_message` deque dedup, `display.render` called exactly once per `tick()`

## 6. Wire Pi entrypoint to shared modules

- [ ] 6.1 Replace `EffectCoordinator` in `heart-matrix-controller/main.py` with `from lib_shared.effects_coordinator import EffectsCoordinator` and an instantiation passing the Pi's `MatrixDisplay`, `scroller`, the effects list, the heartbeat, and `recent_provider=lambda: _message_mgr.get_messages(limit=5)`
- [ ] 6.2 Update pattern imports in `main.py` to `from lib_shared.patterns.{fireworks,flame,nightsky,heartbeat,honeycomb,hyperspace,png_display,video_display} import …`
- [ ] 6.3 Verify Pi boot: `sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 heart-matrix-controller/main.py` starts, renders the heart, fades to the first effect, and scrolls the seeded message (manual check)

## 7. Rename browser display module and add WebDisplay.render

- [ ] 7.1 `git mv` `heart-message-manager/preview_canvas.py` → `preview_display.py`; the file keeps both `WebCanvas` and `WebDisplay`
- [ ] 7.2 Make `WebDisplay` subclass `DisplayBase`; add `WebDisplay.render(effect, scroller)` that does `self.canvas.clear(); effect.render(self.canvas); scroller.render(self.canvas)` (no SwapOnVSync)

## 8. Delete preview_renderer.py and wire browser to shared modules

- [ ] 8.1 Delete `heart-message-manager/preview_renderer.py` (`git rm`); the file's contents (both `PreviewCoordinator` and `PreviewRenderer`) are no longer needed
- [ ] 8.2 Rewrite `heart-message-manager/preview_main.py` to:
  - Import the shared `EffectsCoordinator` from `lib_shared.effects_coordinator`
  - Import `WebCanvas`, `WebDisplay` from `preview_display`
  - Import `PreviewScroller` from `preview_scroller`
  - Import `Fireworks`, `Flame`, `NightSky`, `Honeycomb`, `Hyperspace`, and `Heartbeat` from `lib_shared.patterns` (and their respective modules)
  - Build the effects list by direct instantiation: `effects = [Fireworks(_display), Flame(_display), NightSky(_display), Honeycomb(_display), Hyperspace(_display)]`
  - Build `_heart = Heartbeat(_display)`
  - Build the coordinator: `EffectsCoordinator(_display, _scroller, effects, heart=_heart)` (no `recent_provider` — coordinator uses the internal deque)
  - Keep the JS-bridge surface (`tick`, `request_message`, `get_frame_rgba`, `get_current_effect_name`, `get_current_text`) — those still work via `coordinator.tick()`, `coordinator.request_message()`, `coordinator.current_effect_name`, `coordinator.current_text`

## 9. Update browser symlinks + py-config.toml

- [ ] 9.1 Add symlinks under `heart-message-manager/static/preview/lib_shared/`: `effect_base.py` → `lib_shared/effect_base.py`, `display_base.py` → `lib_shared/display_base.py`, `effects_coordinator.py` → `lib_shared/effects_coordinator.py`, and the six browser-eligible `patterns/{fireworks,flame,nightsky,honeycomb,hyperspace,heartbeat}.py` symlinks
- [ ] 9.2 Remove symlinks under `heart-message-manager/static/preview/heart-matrix-controller/`: `patterns/*` (all 8) and `rgb_display.py`
- [ ] 9.3 Update `heart-message-manager/py-config.toml` `[files]`: add `lib_shared/effect_base.py`, `lib_shared/display_base.py`, `lib_shared/effects_coordinator.py`, and the six browser-eligible `lib_shared/patterns/*` entries (including `heartbeat`); rename `heart-message-manager/preview_canvas.py` to `heart-message-manager/preview_display.py`; remove the old `heart-matrix-controller/patterns/*` and `heart-matrix-controller/rgb_display.py` entries; remove the `heart-message-manager/preview_renderer.py` entry (the file is gone)
- [ ] 9.4 Update `heart-message-manager/preview_main.py` `sys.path` to include `/static/preview/lib_shared` (and drop `/static/preview/heart-matrix-controller` if no other browser file still lives there)

## 10. Wire Hyperspace into the browser preview

- [ ] 10.1 Add `from lib_shared.patterns import fireworks, flame, nightsky, honeycomb, hyperspace` to `heart-message-manager/preview_main.py` and add `Hyperspace(_display)` to the effects list
- [ ] 10.2 Confirm the Hyperspace symlink and `py-config.toml` entry are present (covered by tasks 9.1 and 9.3)
- [ ] 10.3 Verify `Hyperspace` appears in the browser effect cycle (covered by task 8.2's effects list and the manual check below)

## 11. Remove dead config_reader browser symlink

- [ ] 11.1 Verify the browser never imports `config_reader` (`rg -n "config_reader" heart-message-manager/static/preview/ heart-message-manager/preview_main.py heart-message-manager/py-config.toml` returns no matches)
- [ ] 11.2 Remove the `config_reader.py` symlink under `heart-message-manager/static/preview/heart-matrix-controller/` (if present)
- [ ] 11.3 Remove any `config_reader` entry from `py-config.toml` `[files]` (if present)

## 12. Final verification

- [ ] 12.1 Run `PYTHONPATH=. pytest tests/ -v` — all 12 pre-existing test files pass unchanged, plus the four new test files (`effect_base_test.py`, `patterns_import_test.py`, `display_base_test.py`, `effects_coordinator_test.py`) pass
- [ ] 12.2 Run `rg "from patterns\." lib_shared heart-matrix-controller heart-message-manager` — no matches (patterns are imported by full module path now)
- [ ] 12.3 Run `rg "from rgb_display import" heart-matrix-controller heart-message-manager lib_shared` — no matches (the file is renamed to `rgb_matrix_display.py`; the only surviving import is `from rgb_matrix_display import MatrixDisplay`)
- [ ] 12.4 Run `rg "Hyperspace"` — matches include the import line in `preview_main.py` and the entry in `py-config.toml`
- [ ] 12.5 Run `rg "preview_canvas" heart-message-manager/` — no matches (the rename landed)
- [ ] 12.6 Run `rg "preview_renderer" heart-message-manager/` — no matches (the file is deleted)
- [ ] 12.7 Run `rg "class Display\b" heart-matrix-controller/rgb_matrix_display.py` — the only match is the abstract `DisplayBase` parent class import (no concrete `class Display` definition)
- [ ] 12.8 Run `ls heart-matrix-controller/rgb_display.py` — file does not exist (renamed to `rgb_matrix_display.py`)
