## ADDED Requirements

### Requirement: PngDisplay class is renamed to ImageDisplay
The class in `lib_shared/patterns/png_display.py` MUST be renamed to `ImageDisplay` in `lib_shared/patterns/image_display.py`; the module's existing palette-based rendering through the indexed `Bitmap` / `Palette` pipeline MUST be preserved.

#### Scenario: Class and module renamed
- **WHEN** Python imports `from lib_shared.patterns.image_display import ImageDisplay`
- **THEN** the import succeeds and `ImageDisplay` is a subclass of `lib_shared.effect_base.Effect`.

#### Scenario: Old module removed
- **WHEN** `lib_shared/patterns/png_display.py` is referenced
- **THEN** the file does NOT exist (the rename is complete; no shim remains).

#### Scenario: Alphabetical and natural sort preserved
- **WHEN** `ImageDisplay._paths` is built via `glob`
- **THEN** the list is sorted by `_natural_key` (so "Artboard 2" precedes "Artboard 10") — same sort logic as before.

#### Scenario: Effects factory returns ImageDisplay for both names
- **WHEN** `make_effect_class("PngDisplay")` or `make_effect_class("ImageDisplay")` is called
- **THEN** both return the `ImageDisplay` class from the renamed module.

### Requirement: ImageDisplay supports PNG, JPEG, GIF, and WebP
The class MUST load images via PIL/Pillow's `Image.open(path)` and accept any of the file extensions `*.png`, `*.jpg`, `*.jpeg`, `*.gif`, `*.webp` via its directory glob.

#### Scenario: Glob matches PNG and JPEG
- **WHEN** a directory contains `a.png` and `b.jpg`
- **THEN** `ImageDisplay._paths` includes both files.

#### Scenario: Glob excludes unrelated extensions
- **WHEN** a directory contains `a.png`, `b.txt`, `c.mp4`
- **THEN** `ImageDisplay._paths` includes `a.png` only — `b.txt` and `c.mp4` are excluded by the extension tuple filter.

#### Scenario: JPEG loads (no alpha)
- **WHEN** `ImageDisplay._render_image(path_to_b.jpg)` is called
- **THEN** the image is loaded via `Image.open(path).convert("RGB")` (drop alpha), and is quantized into the indexed `Bitmap`/`Palette` for panel rendering.

#### Scenario: PNG with alpha renders with the alpha mask
- **WHEN** `ImageDisplay._render_image(path_to_a.png)` is called and the PNG has a transparent background
- **THEN** the loader uses `img.getchannel("A")` as a white-on-black mask (same as today) — the drawing ink is white, the transparent area is black.

#### Scenario: WebP loads
- **WHEN** `ImageDisplay._render_image(path_to_d.webp)` is called
- **THEN** PIL loads it via the standard WebP plugin path; the result is quantized identically.

#### Scenario: Animated GIF renders as single frame
- **WHEN** `ImageDisplay._render_image` is called on an animated GIF
- **THEN** the loader uses `Image.open(path).convert("RGB")` (collapses animation to first frame); the panel shows the first frame for the duration. The renderer's log line documents that animated GIF is single-frame.

#### Scenario: Corrupt file (decode failure)
- **WHEN** `ImageDisplay._render_image(path_to_corrupt.png)` is called
- **THEN** the loader logs a WARNING, falls back to the existing `self.bitmap = Bitmap(self._w, self._h); self.palette = Palette(1)` (black-on-black blank), and `tick()` continues. The slideshow does NOT crash.

### Requirement: Default EffectsSettings.effects enables ImageDisplay by default
The constant `_DEFAULT_EFFECTS_LIST_FULL` in `lib_shared/models.py` MUST be updated to include `{"name": "ImageDisplay", "enabled": True}` in place of the prior `{"name": "PngDisplay", "enabled": False}` entry.

#### Scenario: Default rotation includes ImageDisplay enabled
- **WHEN** `EffectsSettings()` is constructed with no `effects` arg
- **THEN** the resulting `effects_settings.effects` list includes an entry `{"name": "ImageDisplay", "enabled": True}` and does NOT include any entry with `name == "PngDisplay"`.

#### Scenario: PngDisplay factory alias still resolves
- **WHEN** an existing `EffectsSettings.effects` list (built before this change) contains `{"name": "PngDisplay", "enabled": True}`
- **THEN** `build_effects(effects_settings, display=...)` constructs an `ImageDisplay(display)` for that entry (via the factory alias). No warning, no skip.

#### Scenario: PngDisplay factory alias logs deprecation
- **WHEN** `make_effect_class("PngDisplay")` is called
- **THEN** it returns the `ImageDisplay` class and logs a deprecation WARNING suggesting the operator update their config to use `"ImageDisplay"`.

### Requirement: ImageDisplay crossfade behavior is preserved
The existing two-stage crossfade (hold → out → in with gamma-corrected brightness ramp) MUST be preserved with identical timing defaults.

#### Scenario: Single-image run holds the image
- **WHEN** `ImageDisplay._paths` has length 1
- **THEN** `tick()` immediately returns — the single image stays lit at full brightness forever (no crossfade).

#### Scenario: Multi-image crossfade at default intervals
- **WHEN** `ImageDisplay._paths` has length >= 2 and `PNG_INTERVAL=8.0`, `PNG_FADE=0.6`
- **THEN** each image holds for 8 s, fades out over 0.6 s, swaps to the next image, and fades in over 0.6 s — repeating through all images in `_natural_key` order.

#### Scenario: Brightness from coordinator passes through
- **WHEN** the `EffectsCoordinator` calls `image_display.set_brightness(0.5)`
- **THEN** the palette entries are scaled by the per-image crossfade brightness AND the coordinator's global brightness — both factors multiply. A pure coordinator fade does NOT disturb the per-image fade phase; pure per-image fade does NOT disturb the coordinator's view.
