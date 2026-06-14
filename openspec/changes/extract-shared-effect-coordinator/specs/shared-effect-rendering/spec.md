## ADDED Requirements

### Requirement: Shared effect primitives are rgbmatrix-independent
The system SHALL provide `Bitmap`, `Palette`, `arrayblit`, and the `Effect` base class in `lib_shared/effect_base.py` such that they have no dependency on the `rgbmatrix` package. Importing this module MUST NOT import `rgbmatrix`.

#### Scenario: effect_base imports without rgbmatrix
- **WHEN** `import lib_shared.effect_base` is executed in a process that does not have the `rgbmatrix` package installed
- **THEN** the import succeeds without raising `ModuleNotFoundError` or any other exception

#### Scenario: Effect base class is usable in isolation
- **WHEN** a subclass sets `self.bitmap`, `self.palette`, and calls `self._init_render()` followed by `self.set_brightness(0.5)`
- **THEN** `self.palette` reflects the gamma-corrected fade for every original color

### Requirement: Patterns live in lib_shared
The system SHALL host all rotation and boot-splash effect pattern modules (`fireworks`, `flame`, `nightsky`, `honeycomb`, `hyperspace`, `png_display`, `video_display`, `heartbeat`) in `lib_shared/patterns/`. Each pattern module MUST import its primitives from `lib_shared.effect_base` (not from `rgb_display`).

#### Scenario: All pattern modules import from lib_shared.effect_base
- **WHEN** `rg -n "from rgb_display import" lib_shared/patterns/` is executed
- **THEN** no matches are returned

#### Scenario: No Pi-only pattern directory remains
- **WHEN** `ls heart-matrix-controller/patterns/` is executed
- **THEN** the directory does not exist

### Requirement: DisplayBase defines polymorphic render
The system SHALL provide `DisplayBase` in `lib_shared/display_base.py` with one abstract method, `render(effect, scroller)`, that composites one frame. The base class MUST also declare (concrete or as documented attributes) `clear()`, `width`, `height`, and `canvas` — the surface the effects and the coordinator's per-frame code rely on.

#### Scenario: DisplayBase can be subclassed
- **WHEN** a subclass implements `render(self, effect, scroller)` and is passed to an `EffectsCoordinator` constructed with that subclass as `display`
- **THEN** the coordinator's `tick()` calls `display.render(self.current, self.scroller)` exactly once per frame after the state-machine step

### Requirement: Pi MatrixDisplay subclasses DisplayBase
The Pi's `Display` class SHALL be renamed to `MatrixDisplay` and SHALL live in `heart-matrix-controller/rgb_matrix_display.py` (renamed from `rgb_display.py`). `MatrixDisplay` SHALL subclass `DisplayBase` and implement `render(effect, scroller)` as the existing clear → effect.render → scroller.render → `SwapOnVSync` sequence. `MatrixDisplay` MUST be the only rgbmatrix-importing class in the Pi directory.

#### Scenario: MatrixDisplay.render composites a SwapOnVSync-paced frame
- **WHEN** `display.render(effect, scroller)` is called on the Pi
- **THEN** the final operation is `self._matrix.SwapOnVSync(canvas)` so the panel refresh paces the calling loop

#### Scenario: rgb_matrix_display.py imports rgbmatrix and nothing else from lib_shared
- **WHEN** `rg -n "import rgbmatrix" heart-matrix-controller/` is executed
- **THEN** the only match is in `heart-matrix-controller/rgb_matrix_display.py`
- **WHEN** `rg -n "from lib_shared" heart-matrix-controller/rgb_matrix_display.py` is executed
- **THEN** only the `DisplayBase` import is returned (no re-export of `Bitmap` / `Palette` / `arrayblit` / `Effect`)

#### Scenario: Old names are gone
- **WHEN** `ls heart-matrix-controller/rgb_display.py` is executed
- **THEN** the file does not exist (renamed to `rgb_matrix_display.py`)
- **WHEN** `rg -n "class Display\b" heart-matrix-controller/rgb_matrix_display.py` is executed
- **THEN** the only match is the abstract parent class import (no concrete `class Display` definition)

### Requirement: Browser WebDisplay subclasses DisplayBase
The browser's `WebDisplay` SHALL subclass `DisplayBase` and implement `render(effect, scroller)` as `self.canvas.clear(); effect.render(self.canvas); scroller.render(self.canvas)` (no `SwapOnVSync` — the browser's rAF loop paces itself).

#### Scenario: WebDisplay.render composites a clear+render+render frame
- **WHEN** `display.render(effect, scroller)` is called in the browser
- **THEN** the operations in order are `self.canvas.clear()`, `effect.render(self.canvas)`, `scroller.render(self.canvas)` — and no `SwapOnVSync` is performed

### Requirement: EffectsCoordinator is shared by both subsystems
The system SHALL provide a concrete `EffectsCoordinator` class in `lib_shared/effects_coordinator.py` that owns the full lifecycle state machine (`intro` → `out` → `in` → `hold` → `text_out` → `background` → repeat), the brightness-ramp state (`fade_start`, `last_step`, `fade_step`, `gamma`), the recent-message source (either a `recent_provider` callable or an internal deque), and the public API (`start`, `request_message`, `tick`, `current_effect_name`, `current_text`). The class is **directly usable** by both the Pi entrypoint (`heart-matrix-controller/main.py`) and the browser entrypoint (`heart-message-manager/preview_renderer.py`) with no subclass.

#### Scenario: Mode transitions follow the documented lifecycle
- **WHEN** an `EffectsCoordinator` is constructed with a stub display, scroller, effects, and heart, `start(None)` is called, and `tick()` is advanced through `intro_seconds + fade_seconds + fade_seconds` of monotonic time
- **THEN** the mode progresses `intro → out → in → background` and the active effect has been advanced from `heart` to `effects[0]`

#### Scenario: Brightness ramp endpoints are reached
- **WHEN** a fade-out completes (`progress >= 1.0`)
- **THEN** the active effect's `set_brightness(0.0)` was called as the final step
- **WHEN** a fade-in completes
- **THEN** the active effect's `set_brightness(1.0)` and the scroller's `set_brightness(1.0)` were called as the final step

#### Scenario: idx advances on every fade-out complete
- **WHEN** the coordinator's `out` mode completes N times
- **THEN** `self.idx` has been advanced by N (modulo `len(self.effects)`)

#### Scenario: pending_text is consumed on the out→in transition
- **WHEN** `request_message("hello")` is called while in `background` mode
- **THEN** `self.pending_text == "hello"` and the next `out → in` transition consumes it (sets `self.pending_text = None`, `self.last_shown_text = "hello"`, `self.scroller` was given the text "hello")

#### Scenario: New message during hold interrupts
- **WHEN** the coordinator is in `hold` mode and `request_message("new")` is called
- **THEN** the next `tick()` transitions to `out` mode (not waiting for `hold_seconds` to elapse)

#### Scenario: request_message dedupes the internal deque
- **WHEN** `request_message("hello")` is called twice in a row and no `recent_provider` is configured
- **THEN** the internal `_recent` deque contains `"hello"` exactly once

#### Scenario: Tick calls display.render exactly once
- **WHEN** a stub display records each `render` call and `tick()` is invoked
- **THEN** the stub display records exactly one `render(self.current, self.scroller)` call per `tick()`

### Requirement: Coordinator instance is configurable per subsystem
The Pi and the browser each instantiate `EffectsCoordinator` with subsystem-specific arguments. The Pi's instantiation passes a `recent_provider` callable (e.g. `lambda: message_manager.get_messages(limit=5)`). The browser's instantiation passes `recent_provider=None` and lets the coordinator use its internal deque. The coordinator's class is the same in both cases.

#### Scenario: Pi coordinator uses recent_provider
- **WHEN** an `EffectsCoordinator` is constructed with `recent_provider=lambda: [entries]` and `_random_recent()` is called
- **THEN** the function reads from the `recent_provider` callable's return value (not from the internal deque)

#### Scenario: Browser coordinator uses internal deque
- **WHEN** an `EffectsCoordinator` is constructed with `recent_provider=None` and `request_message("hi")` is called followed by `_random_recent()`
- **THEN** the function returns `"hi"` from the internal deque

### Requirement: Hyperspace is in the browser preview
The browser preview's effect cycle SHALL include `Hyperspace`. The pattern MUST be imported in `preview_main.py` (from `lib_shared.patterns`), listed in `py-config.toml` `[files]`, and reachable via a symlink at `heart-message-manager/static/preview/lib_shared/patterns/hyperspace.py`.

#### Scenario: Hyperspace appears in the browser cycle
- **WHEN** the browser preview boots
- **THEN** a `Hyperspace` instance is in the effects list passed to `EffectsCoordinator`

#### Scenario: Hyperspace symlink resolves to the shared module
- **WHEN** `readlink heart-message-manager/static/preview/lib_shared/patterns/hyperspace.py` is executed
- **THEN** the resolved path is `lib_shared/patterns/hyperspace.py` (relative to the repo root)

### Requirement: Dead config_reader browser symlink is removed
The filesystem symlink at `heart-message-manager/static/preview/heart-matrix-controller/config_reader.py` (or any equivalent path that the browser could resolve to it) MUST be removed because the browser never imports `config_reader`.

#### Scenario: No browser code path references config_reader
- **WHEN** `rg -n "config_reader" heart-message-manager/static/preview/ heart-message-manager/preview_main.py heart-message-manager/py-config.toml` is executed
- **THEN** no matches are returned

### Requirement: preview_renderer.py is deleted
The file `heart-message-manager/preview_renderer.py` MUST be deleted. It currently contains `PreviewCoordinator` (replaced by the shared `EffectsCoordinator`) and `PreviewRenderer` (effect-construction with try/except skipping; replaced by direct instantiation in `preview_main.py`). The browser entrypoint MUST build its effect list by directly instantiating the patterns it uses (Fireworks, Flame, NightSky, Honeycomb, Hyperspace) and passing them to the shared `EffectsCoordinator`.

#### Scenario: preview_renderer.py does not exist
- **WHEN** `ls heart-message-manager/preview_renderer.py` is executed
- **THEN** the file does not exist

#### Scenario: No browser code path references preview_renderer
- **WHEN** `rg -n "preview_renderer" heart-message-manager/` is executed
- **THEN** no matches are returned (the file is gone and nothing imports it)

#### Scenario: preview_main.py imports the shared coordinator
- **WHEN** `rg -n "from lib_shared.effects_coordinator" heart-message-manager/preview_main.py` is executed
- **THEN** exactly one match is returned for the `EffectsCoordinator` import

#### Scenario: Browser effects are built by direct instantiation
- **WHEN** `heart-message-manager/preview_main.py` is read
- **THEN** there is no `PreviewRenderer` instantiation; the effects list is built by calling `Fireworks(_display)`, `Flame(_display)`, `NightSky(_display)`, `Honeycomb(_display)`, and `Hyperspace(_display)` directly

### Requirement: Heartbeat is the browser boot splash
The browser's `preview_main.py` MUST instantiate `Heartbeat` from `lib_shared.patterns.heartbeat` and pass it to `EffectsCoordinator` as the `heart` parameter. The browser's boot splash MUST show the beating heart for `intro_seconds` before fading to the first rotation effect — mirroring the Pi's behavior.

#### Scenario: Heartbeat is in the browser module imports
- **WHEN** `rg -n "from lib_shared.patterns.heartbeat" heart-message-manager/preview_main.py` is executed
- **THEN** a match is returned for the Heartbeat import

#### Scenario: Heartbeat is passed as the heart parameter
- **WHEN** `heart-message-manager/preview_main.py` is read
- **THEN** the `EffectsCoordinator` constructor receives `heart=_heart` where `_heart = Heartbeat(_display)`

### Requirement: Browser py-config.toml mirrors the shared module layout
`heart-message-manager/py-config.toml` `[files]` MUST list `lib_shared/effect_base.py`, `lib_shared/display_base.py`, `lib_shared/effects_coordinator.py`, and the six browser-eligible pattern files (`fireworks`, `flame`, `nightsky`, `honeycomb`, `hyperspace`, `heartbeat`). It MUST NOT list `heart-matrix-controller/patterns/*` or `heart-matrix-controller/rgb_display.py`.

#### Scenario: py-config.toml lists the new files
- **WHEN** `grep -E "lib_shared/(effect_base|display_base|effects_coordinator|patterns/)" heart-message-manager/py-config.toml` is executed
- **THEN** matches include `lib_shared/effect_base.py`, `lib_shared/display_base.py`, `lib_shared/effects_coordinator.py`, and all six browser-eligible patterns (including `heartbeat`)

#### Scenario: py-config.toml no longer lists the old files
- **WHEN** `grep -E "heart-matrix-controller/(patterns|rgb_display)" heart-message-manager/py-config.toml` is executed
- **THEN** no matches are returned

### Requirement: Browser display module is named preview_display.py
The browser's display module MUST be named `heart-message-manager/preview_display.py` (renamed from `preview_canvas.py`). The file SHALL contain both `WebCanvas` (the Pillow-backed rgbmatrix-canvas shim) and `WebDisplay` (the `DisplayBase` subclass). All imports in the browser code path that previously referenced `preview_canvas` MUST reference `preview_display`.

#### Scenario: preview_canvas.py is gone
- **WHEN** `ls heart-message-manager/preview_canvas.py` is executed
- **THEN** the file does not exist

#### Scenario: preview_display.py contains both classes
- **WHEN** `heart-message-manager/preview_display.py` is imported
- **THEN** both `WebCanvas` and `WebDisplay` are present as classes in the module

#### Scenario: Browser code imports preview_display
- **WHEN** `rg -n "preview_canvas" heart-message-manager/` is executed
- **THEN** no matches are returned (the new path is `preview_display`)

### Requirement: EffectsCoordinator is test-covered
The system SHALL provide `tests/effects_coordinator_test.py` that exercises the `EffectsCoordinator` state machine through the documented `intro → out → in → background` lifecycle, the `idx` advance, the brightness-ramp endpoints, the `pending_text` consume-on-transition behavior, and the `display.render` call per `tick()`.

#### Scenario: State machine test file exists and passes
- **WHEN** `PYTHONPATH=. pytest tests/effects_coordinator_test.py -v` is executed
- **THEN** all test cases in that file pass

#### Scenario: Existing test suite remains green
- **WHEN** `PYTHONPATH=. pytest tests/ -v` is executed
- **THEN** all 12 pre-existing test files pass unchanged plus the new `effects_coordinator_test.py` (and the additional `effect_base_test.py` and `patterns_import_test.py`) pass
