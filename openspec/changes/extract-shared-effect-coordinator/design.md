## Context

The Pi device (`heart-matrix-controller/main.py`) and the browser preview (`heart-message-manager/preview_renderer.py`) both implement a six-mode fade state machine for the sign's lifecycle (`intro → out → in → hold → text_out → background`). Today, that state machine is duplicated verbatim: every transition, every brightness-ramp calculation, every throttling decision is implemented twice and is already drifting (e.g. the browser's `start()` does a `startup_text not in self._recent` dedup the Pi doesn't). When a new mode is added or a transition bug is fixed, both copies need to be updated in lockstep, and the only signal that the two are out of sync is the divergence itself.

On top of that, the seven rotation patterns and the `Bitmap` / `Palette` / `Effect` / `arrayblit` primitives they all depend on live in `heart-matrix-controller/`, even though none of them touch `rgbmatrix` (only `Display` does). The browser preview reaches them via filesystem symlinks under `static/preview/heart-matrix-controller/`, which means every move/rename has to be replicated in the symlink tree, and `Hyperspace` (which has no browser-specific blocker) was simply forgotten when the preview was wired up.

The full plan is documented in `Plans/let-s-just-do-some-wise-river.md` — this design distills the architectural decisions.

## Goals / Non-Goals

**Goals:**

- A single source of truth for the lifecycle state machine, shared by both the Pi device and the browser preview, with **zero per-environment override surface** on the coordinator.
- The composite step (clear canvas → draw effect → draw scroller → swap) is polymorphism on the **display**, not the coordinator. The coordinator ends with one line: `self.display.render(self.current, self.scroller)`.
- The seven rotation patterns and the `Bitmap` / `Palette` / `Effect` / `arrayblit` primitives they need live in `lib_shared/` with no rgbmatrix dependency, so the browser can import them directly and a Pi refactor can't accidentally break the browser (or vice versa).
- `Hyperspace` is available in the browser preview — the work to do that is wire-up only, since it shares the same primitives as the patterns already supported.
- A minimal pytest covers the state machine, so future mode changes have a test surface.

**Non-Goals:**

- Adding `PngDisplay` or `VideoDisplay` to the browser preview. Both have real blockers (filesystem assets, OpenCV in Pyodide) that are out of scope for a consolidation refactor. (See "Out of scope" in the proposal.)
- Replacing the 3s polling cadence in the browser with a WebSocket push — orthogonal to consolidation.
- Adding new patterns. Only the existing eight files move.
- Refactoring `PreviewScroller`, `WebCanvas`, or `PreviewRenderer` (effect-construction with try/except skipping for browser-incompatible patterns). These are browser-specific by design and the issue body explicitly calls them out as "duplications that AREN'T worth refactoring."

## Decisions

### 1. New `lib_shared/effect_base.py` — primitives module

**Decision:** Move `Bitmap`, `Palette`, `arrayblit`, and the `Effect` base class verbatim from `heart-matrix-controller/rgb_display.py` into a new `lib_shared/effect_base.py`. The rgbmatrix-backed `Display` class is renamed to `MatrixDisplay` and stays in `heart-matrix-controller/rgb_display.py` (now `rgb_matrix_display.py`); the file no longer contains the moved primitives — no re-export shim is needed.

**Rationale:** The rename makes the matrix-backed nature of the class explicit and matches the upstream `rgbmatrix.RGBMatrix` naming convention. The file is renamed `rgb_matrix_display.py` for the same reason. The only `from rgb_display import …` line that survives the refactor is `main.py`'s `from rgb_matrix_display import MatrixDisplay`. The patterns change to `from lib_shared.effect_base import …`; nothing else in the Pi imports the moved primitives from `rgb_display`. So `rgb_matrix_display.py` can be a small file with just `MatrixDisplay` (and the rgbmatrix import it needs) — clean module boundaries, no import indirection, no shim.

**Alternatives considered:**

- *Re-export shim in `rgb_matrix_display.py`.* A no-op: it would re-export symbols that nothing imports. Adds a layer of indirection for no caller.
- *Move `MatrixDisplay` into `lib_shared/display_base.py` too.* Tempting, but `MatrixDisplay` imports `rgbmatrix`, which the browser can't import. `MatrixDisplay` belongs in the Pi directory; the abstract `DisplayBase` belongs in `lib_shared/`.
- *Keep the name `Display` and the file name `rgb_display.py`.* Confusing once `WebDisplay` exists in the browser — two different `Display`s with no naming hint about which is which.

### 2. New `lib_shared/display_base.py` — `DisplayBase` polymorphism

**Decision:** Introduce a `DisplayBase` class in `lib_shared/display_base.py` with one abstract method:

```python
def render(self, effect, scroller):
    """Composite one frame: clear, draw the active effect, draw the scroller, swap."""
    raise NotImplementedError
```

The Pi's `Display` (in `heart-matrix-controller/rgb_display.py`) subclasses `DisplayBase` and implements `render` as `clear → effect.render → scroller.render → SwapOnVSync` (its current behavior). The browser's `WebDisplay` (in the renamed `heart-message-manager/preview_display.py`) implements `render` as `clear → effect.render → scroller.render` (no SwapOnVSync — the browser's rAF loop paces itself).

The base class also declares the `clear()` method, `width`, `height`, and `canvas` attributes the effects and the coordinator's per-frame code need. These are not abstract — both subclasses already implement them.

**Rationale:** The composite is fundamentally a display concern, not a coordinator concern. Putting polymorphism on the display collapses the coordinator to zero per-environment behavior and removes the `_composite` hook that the previous design had. The coordinator's `tick()` ends with the same one line in both subsystems:

```python
self.display.render(self.current, self.scroller)
```

**Alternatives considered:**

- *Keep the composite inline in each coordinator (`tick()` calls `display.canvas.clear()` + `effect.render(...)` + `scroller.render(...)` directly).* This is the current state — the composite is open-coded in `preview_renderer.PreviewCoordinator.tick()`. Adding `WebDisplay.render` is a one-method addition that the coordinator can call uniformly. Polymorphism on the display is the right place.
- *Put the composite on `DisplayBase` as a concrete template method that calls abstract `swap()` / `clear()`.* Over-engineered — both implementations have the same clear+draw+draw sequence; only the swap step differs and one of them (browser) has no swap.

### 3. New `lib_shared/effects_coordinator.py` — concrete shared coordinator

**Decision:** A single concrete class `EffectsCoordinator` lives in `lib_shared/effects_coordinator.py`. It is the **merged** version of the current Pi `EffectCoordinator` and browser `PreviewCoordinator` — same state machine, same fade ramp, same lifecycle modes, same `start` / `request_message` / `tick` / `current_effect_name` / `current_text` public API. There is no `CoordinatorBase` and no subclass.

The class is constructed with a `display`, a `scroller`, a list of `effects`, a `heart` (for the boot splash), and an optional `recent_provider` callable:

```python
EffectsCoordinator(
    display=display,             # any DisplayBase subclass (Pi MatrixDisplay, browser WebDisplay)
    scroller=scroller,
    effects=effects,
    heart=heartbeat,
    recent_provider=lambda: _message_mgr.get_messages(limit=5),  # Pi path
    # recent_provider=None by default — coordinator uses an internal deque (browser path)
)
```

`tick()` ends with `self.display.render(self.current, self.scroller)`. There is no per-environment code path on the coordinator.

The Pi's `main.py` instantiates it with the Pi's `MatrixDisplay` and the `MessageManager` callback. The browser's `preview_main.py` instantiates it with the browser's `WebDisplay` and no `recent_provider` (the internal `_recent` deque is populated by `request_message` calls). `PreviewCoordinator` is gone.

**Rationale:** The coordinator's only "browser-specific" behavior was the `_last_text` dedup in `request_message` (don't store a duplicate of the most recent body in the deque). The dedup is a no-op for the Pi (it gets deduplicated messages from `MessageManager`) and harmless to add to the shared implementation, so we add it once. The read-only properties (`current_effect_name`, `current_text`) are pure functions of `self.current` and `self.scroller.text` — they live on the shared class.

The result: the coordinator is **directly usable by both entrypoints**. No subclass. No hook. No override surface. The browser builds its effect list by direct instantiation in `preview_main.py` (no `PreviewRenderer` wrapper) — the rotation cycle is a fixed list of patterns known to work in the browser, with no try/except hedging because there's nothing to skip.

**Alternatives considered:**

- *Keep `CoordinatorBase` + thin subclasses.* The previous design. Adds an override surface that has exactly one job (composite) — and the composite doesn't belong on the coordinator.
- *Keep `PreviewRenderer` (effect-construction with try/except skipping).* The current state. The hedge protects against "a pattern's constructor raises at import time" — but in the browser, the patterns we import are the ones we know work (Fireworks, Flame, NightSky, Honeycomb, Hyperspace, Heartbeat), and we never import PngDisplay/VideoDisplay at all. There's nothing to skip; the wrapper is dead weight.
- *Make the coordinator a `dataclass` of pure functions and let each subsystem wrap it.* Splits the state machine across two files, which is the duplication we're trying to eliminate.
- *Keep two coordinator classes that share a common mixin.* A mixin is just a base class wearing a costume; we'd be back to the same shape with a worse name.

### 4. New `lib_shared/patterns/` — pattern collection

**Decision:** Move all eight pattern files (`fireworks.py`, `flame.py`, `nightsky.py`, `honeycomb.py`, `hyperspace.py`, `png_display.py`, `video_display.py`, and `heartbeat.py`) from `heart-matrix-controller/patterns/` to `lib_shared/patterns/`. Update each file's `from rgb_display import …` to `from lib_shared.effect_base import …`. Delete the now-empty `heart-matrix-controller/patterns/` directory.

The proposal's "seven files" list was a counting mistake — `heartbeat.py` is also in that directory, has no rgbmatrix dependency, and would otherwise leave a non-empty `heart-matrix-controller/patterns/` blocking the directory removal. The browser preview doesn't import `heartbeat` (the Pi's `EffectsCoordinator` uses it for the boot splash only), so it moves but only the Pi imports it.

**Rationale:** A pattern directory with eight files in Pi-land and a parallel one elsewhere is exactly the duplication we're collapsing. All eight have the same import shape (`from lib_shared.effect_base import …`), so the move is uniform.

**Alternatives considered:**

- *Keep `heartbeat.py` in the Pi directory (since only the Pi uses it).* Leaves `heart-matrix-controller/patterns/` with one file, which is a worse signal than the directory being empty. Also makes the rule "patterns that have no rgbmatrix dep go in lib_shared" hold uniformly.

### 5. Rename `heart-message-manager/preview_canvas.py` → `preview_display.py`

**Decision:** Rename the file. The current `preview_canvas.py` already contains both `WebCanvas` (Pillow-backed rgbmatrix-canvas shim) and `WebDisplay` (the wrapper that exposes `canvas` / `width` / `height` to the patterns). After this change it also gets a `WebDisplay.render(effect, scroller)` method, which makes `WebDisplay` a proper `DisplayBase` subclass. The file's primary purpose is the browser's display, not the canvas; the canvas is one internal piece. The rename is a pure path change with no behavior change beyond the new `render` method.

Update `py-config.toml` `[files]` and the browser symlink tree (currently there isn't one for `preview_canvas.py` — it's served via Flask static, not a symlink). Update `preview_main.py`'s import of `preview_canvas` to `preview_display`. Update any test file that imports from the old path.

**Rationale:** The file is the browser's display module, not its canvas module. Calling it `preview_display.py` matches what it does and what it contains. The class name `WebCanvas` stays inside the file because the canvas is a real abstraction (the rgbmatrix-canvas API shim) that lives within the display module.

**Alternatives considered:**

- *Leave the file as `preview_canvas.py` even though it has both classes.* Names the file after one of its two classes. Confusing — the file's most important class is `WebDisplay` and adding `render()` to `WebDisplay` makes the canvas-only name even more wrong.
- *Split into two files (`preview_canvas.py` with `WebCanvas` and `preview_display.py` with `WebDisplay`).* Two tiny files in a directory that already has `preview_main.py`, `preview_renderer.py`, `preview_scroller.py`. Adds a file for no functional reason.

### 6. Browser symlinks + `py-config.toml` rewired; `preview_renderer.py` deleted

**Decision:** Update `heart-message-manager/static/preview/` symlinks to point at `lib_shared/effect_base.py`, `lib_shared/display_base.py`, `lib_shared/effects_coordinator.py`, and the six browser-eligible `lib_shared/patterns/*` files (Fireworks, Flame, NightSky, Honeycomb, Hyperspace, **and Heartbeat** — the boot-splash pattern, which the browser already uses as the boot splash via the `heart` parameter; we just move its import to the standard `lib_shared.patterns.heartbeat` path). Remove the symlinks for the seven `heart-matrix-controller/patterns/*` files and `heart-matrix-controller/rgb_display.py`. Also remove the dead `heart-matrix-controller/config_reader.py` symlink (the browser never imports it — verified).

Update `heart-message-manager/py-config.toml` `[files]` accordingly: add the new entries, drop the old ones, and rename `heart-message-manager/preview_canvas.py` to `heart-message-manager/preview_display.py`.

Update `heart-message-manager/preview_main.py` imports to:
- `from lib_shared.effects_coordinator import EffectsCoordinator`
- `from lib_shared.patterns import fireworks, flame, nightsky, honeycomb, hyperspace`
- `from lib_shared.patterns.heartbeat import Heartbeat`
- (no more `from preview_renderer import …` — that file is gone)

**`preview_renderer.py` is deleted entirely.** It currently contains `PreviewCoordinator` (eliminated — replaced by the shared `EffectsCoordinator`) and `PreviewRenderer` (eliminated — the browser builds its effect list by direct instantiation in `preview_main.py`, no try/except hedging because there's nothing to skip). The `_BROWSER_COMPATIBLE_PATTERNS` and `_BROWSER_SKIPPED_PATTERNS` constants either become inline imports in `preview_main.py` (for the patterns we use) or are dropped (for the patterns we don't import).

**Rationale:** Symlinks are how PyScript/Pyodide learns about which files to fetch into the browser tab. The browser symlink tree needs to mirror the new module layout exactly. Drop the dead `config_reader.py` symlink because it was always wrong — config is server-side only. `preview_renderer.py` was 200+ lines of browser-specific glue for two classes that no longer exist; deleting it leaves the browser with three small files: `preview_main.py` (entrypoint + JS bridge), `preview_display.py` (canvas + display), `preview_scroller.py` (browser scroller).

**Alternatives considered:**

- *Keep `PreviewRenderer` for future-proofing (in case more browser-incompatible patterns are added).* Speculative. The Pi imports all 8 patterns; the browser explicitly imports only the ones that work. If a future pattern is browser-incompatible, we just don't list it in the browser's import block — no wrapper needed.
- *Add a HTTP route that serves the new `lib_shared/patterns/*` files dynamically and stop using symlinks for the patterns.* Cleaner, but PyScript's `py-config.toml [files]` is a static list and the project's existing pattern is symlinks. Stay consistent with the existing pattern.

### 7. Hyperspace wire-up is part of this change

**Decision:** Add `lib_shared/patterns/hyperspace.py` to `py-config.toml` `[files]`, add the corresponding symlink under `static/preview/lib_shared/patterns/`, and add `from lib_shared.patterns import fireworks, flame, nightsky, honeycomb, hyperspace` to `preview_main.py` (Hyperspace is in the rotation cycle just like the other five).

**Rationale:** Hyperspace was skipped during the original preview wire-up (likely a copy/paste oversight when the symlink list was assembled). It uses the same `arrayblit` and abstract canvas primitives as the other supported patterns — verified by reading the file. No code change is required to support it; this is purely wiring.

### 8. `tests/effects_coordinator_test.py`

**Decision:** A small pytest that constructs an `EffectsCoordinator` with a fake display/scroller/effects/heart, drives it through controlled `time.monotonic` advances, and asserts:

- Mode transitions follow `intro → out → in → background` (using `start` to enter `intro`, then advancing time).
- `idx` advances on fade-out complete.
- Brightness ramp endpoints: at `progress=0`, the current effect's `set_brightness(0)` was called; at `progress=1`, `set_brightness(1)` was called.
- `pending_text` is set by `request_message`, consumed on the next `out → in` transition, and the next advance doesn't carry it.
- `display.render` is called once per `tick()` after the state-machine step (using a stub display that records calls).

The test uses a stub `display` (records `render` calls) and stub `scroller` / `effect` so it stays a pure state-machine test. ~30–50 lines.

**Rationale:** A state machine this size warrants a test surface — every bug in the past has been a mode-transition edge case. Even a thin test catches the obvious regressions (forgetting to reset `last_step`, off-by-one on `idx`, missing a `display.render` call).

## Risks / Trade-offs

- **Import path churn in eight pattern files** — each `from rgb_display import …` becomes `from lib_shared.effect_base import …`. Mechanical, but every pattern file touches. Mitigation: the patterns are touched once each, not the call sites.
- **Browser preview regression risk** — moving pattern files changes how PyScript/Pyodide resolves them. Mitigation: keep the symlink tree 1:1 with the new module layout, update `py-config.toml` to match, and verify with the manual checks in the proposal's "Verification" section (`/preview` page loads, all five effects cycle, status block renders). The existing 12 pytest files don't touch the browser preview directly, so the green-light signal is manual, but the change set is small and the wire-up is mechanical.
- **Test file is intentionally thin** — covering the state machine at a high level, not exhaustive path coverage. Mitigation: the test catches the gross regressions (mode stuck, brightness inverted, `idx` not advancing, `display.render` not called). Edge cases (idle reset, `_random_recent` no-repeat) are simple enough that the implementation reads clearly.
- **Heartbeat pattern moves to `lib_shared` even though only the Pi imports it** — the browser has its own boot-splash behavior (the `_BROWSER_COMPATIBLE_PATTERNS` list doesn't include it). Slight directory bloat. Mitigation: it's one file, and the rule "patterns that have no rgbmatrix dep go in lib_shared" is uniform. The alternative (Pi-specific directory with one file) is a worse signal.
- **The merged `EffectsCoordinator` is one file rather than a base + two subclasses** — fewer types in the codebase, but a single change to the state machine affects both subsystems. Mitigation: that's the point — the state machine should not be a per-subsystem concern. The test (`tests/effects_coordinator_test.py`) catches regressions that would have manifested as drift between two near-identical implementations.

## Migration Plan

This is a refactor — no data migration, no deployment coordination. The steps are:

1. Land `lib_shared/effect_base.py`, `lib_shared/display_base.py`, `lib_shared/effects_coordinator.py`, and `lib_shared/patterns/` in a single commit (or a small sequence if review needs to be split).
2. Update the eight pattern files' imports in the same commit.
3. Rename `heart-matrix-controller/rgb_display.py` → `rgb_matrix_display.py`; rename `Display` → `MatrixDisplay`; strip the moved primitives (no shim); make `MatrixDisplay` subclass `DisplayBase` and implement `render` (its current behavior).
4. Replace `heart-matrix-controller/main.py`'s `EffectCoordinator` with an import + instantiation of the shared `EffectsCoordinator`.
5. Rename `heart-message-manager/preview_canvas.py` → `preview_display.py`; add `WebDisplay.render(effect, scroller)` to make it a `DisplayBase` subclass.
6. Replace `heart-message-manager/preview_renderer.py` (delete the file): drop `PreviewCoordinator` (replaced by the shared `EffectsCoordinator`) and `PreviewRenderer` (effect list is now built by direct instantiation in `preview_main.py`).
7. Update browser symlinks, `py-config.toml`, and `preview_main.py` imports.
8. Add `tests/effects_coordinator_test.py` (and `tests/effect_base_test.py`, `tests/patterns_import_test.py`, `tests/display_base_test.py` for the primitives + pattern construction surface + display polymorphism).
9. Run `PYTHONPATH=. pytest tests/ -v` — all 12 existing test files + the new ones pass.
10. Manual verification: boot the Flask server, open `/preview`, confirm the boot splash is the beating heart, the effect cycle advances through Fireworks, Flame, NightSky, Honeycomb, **and Hyperspace**; new message triggers fade; status block shows the active effect name and current text. Manual Pi-side: `sudo python3 heart-matrix-controller/main.py` boots, renders, scrolls a test message.

Rollback is a single `git revert` of the merge commit.

## Open Questions

None blocking. The one judgment call is whether to also rename `preview_main.py` for consistency with the `preview_display.py` and `preview_scroller.py` siblings — it stays as `preview_main.py` because it's the entrypoint, not a render-path component, and the "main" suffix is the conventional Flask/PyScript entrypoint naming.
