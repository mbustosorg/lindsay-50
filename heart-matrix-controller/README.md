# Heart Matrix Controller

Runs on a Raspberry Pi with a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine-wired). Receives messages via MQTT from the Flask server and renders scrolling text over animated background effects.

## Hardware

- **Panel**: 64×64 RGB HUB75 LED matrix (two 64×32 panels chained)
- **Controller**: Raspberry Pi with [hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library
- **Configuration**: `settings.toml` or environment variables

## Pi deployment

The Pi needs one operator-provided file: `settings.toml` (MQTT creds,
panel geometry, log level, etc.). Everything else ships in the repo.
The `scripts/provision-pi.sh` flow ships it + bootstraps the Pi in one
shot — no manual `scp` + re-run cycle.

### One-time provisioning (from your laptop)

Run `scripts/provision-pi.sh` from the repo root on your laptop, with
your filled-in `heart-matrix-controller/settings.toml` in place:

```bash
scripts/provision-pi.sh root@lindsay-50
```

The script:

1. Detects the local repo (cwd has `.git` + `heart-matrix-controller/`).
2. Verifies `<cwd>/heart-matrix-controller/settings.toml` exists
   locally — fails fast with a clear message if not.
3. Pre-flights SSH to the Pi (so a typo'd hostname fails before any
   destructive work).
4. SSHes in, clones the repo at `$LINDSAY50_PI_REPO_DIR` (default
   `/srv/lindsay-50`), and checks out the current HEAD of your
   laptop checkout.
5. Scps the local `settings.toml` onto the Pi at the canonical
   `<repo_dir>/heart-matrix-controller/settings.toml` (atomic
   `scp-to-.tmp` then `mv`).
6. SSHes in once more to run `setup-pi.sh`, which is the
   authoritative on-Pi bootstrap (apt + pip → bare repo + per-version
   worktree → systemd).

Expected downtime: 5–10 min on a fresh Pi (rgbmatrix C build);
under 5 seconds on an already-bootstrapped Pi.

### Configuration via env vars

If the defaults don't match your setup, override via env vars
(positional arg `PI_HOST` takes precedence over `LINDSAY50_PI_HOST`):

| Var | Default | Purpose |
|---|---|---|
| `LINDSAY50_PI_HOST` | `root@lindsay-50` | SSH target |
| `LINDSAY50_PI_REPO_DIR` | `/srv/lindsay-50` | Where the Pi keeps the repo |
| `LINDSAY50_LOCAL_SETTINGS` | `<cwd>/heart-matrix-controller/settings.toml` | Where you keep the canonical copy |
| `LINDSAY50_GIT_REF` | `HEAD` of cwd | Commit / branch the Pi should run |

Example: pointing at a non-default settings path:

```bash
LINDSAY50_LOCAL_SETTINGS=~/secrets/lindsay-50/settings.toml \
    scripts/provision-pi.sh root@lindsay-50
```

### Running `setup-pi.sh` directly on the Pi

You normally don't need to. But if you want to bootstrap the Pi
without going through `provision-pi.sh` (e.g. headless, no laptop
involved), `setup-pi.sh` still hard-stops with a clear message if
`settings.toml` is missing at the canonical path — scp it in and
re-run.

### Subsequent version bumps

After the first setup, every `git worktree add` (whether triggered by
`setup-pi.sh`, the upgrade flow in `loader.py`, or manually) fires
`hooks/post-checkout`, which calls `scripts/sync_settings.sh` to copy
the canonical `settings.toml` into the new `v-<sha>/` worktree. You
only need to drop a fresh `settings.toml` at the canonical Pi path
when your settings **change**; you do **not** have to re-provision on
every version bump.

To force-refresh an existing worktree (e.g. after a settings change
that didn't survive a worktree swap), just re-run the laptop-side
provisioner — it ships the file and re-runs `setup-pi.sh`, both
idempotent.

## Architecture

```
SMS → Twilio → Flask ──MQTT──→ ESP32 (CircuitPython)
                                  │
                              MQTT broker
                                  │
                    ┌─────────────┴──────────────┐
                    │  Raspberry Pi (this code)  │
                    │                            │
                 Display                     MessageManager
              (RGBMatrix)                        │
                    │                     on_message callback
              Canvas (double-buffer)             │
                    │                      EffectCoordinator
               ┌────┴────┐                       │
           Effect     Scroller                   │
         (bitmap)   (BDF font)                   │
              │          │                       │
           Palette    DrawText                   │
              │          │                       │
              └────┬─────┘                       │
                   │                             │
              SwapOnVSync ←─── main loop ────────┘
```

## Rendering Pipeline

### 1. Display (rgb_display.py)

Owns the RGBMatrix and a double-buffered canvas (`CreateFrameCanvas`). Each frame:

1. `canvas.Clear()` — blank the offscreen buffer
2. `effect.render(canvas)` — blit the active effect's pixels
3. `scroller.render(canvas)` — draw scrolling text
4. `SwapOnVSync(canvas)` — atomically flip to the new frame (blocks until panel's vertical refresh)

`SwapOnVSync` paces the main loop — no `time.sleep` needed.

### 2. Bitmap and Palette (rgb_display.py)

Effects write pixels using a **palette index** into a flat `bytearray` (`Bitmap`), not raw RGB values. A separate `Palette` maps each index to an `0xRRGGBB` color.

```
Bitmap:  [idx, idx, idx, ...]   ← one byte per pixel, row-major
Palette:  [0x000000, 0xFF0000, 0x00FF00, ...]  ← index → color
```

This mirrors CircuitPython's `displayio.Bitmap` / `displayio.Palette` API. Effects are portable between the CircuitPython ESP32 version and this Pi version.

**Accessing pixels**: `bitmap[x, y]` — Python converts the comma-separated indices to a tuple `(x, y)` and calls `__getitem__(xy=(x, y))`, which unpacks to `x, y = xy`. Pixel at `(x, y)` lives at flat index `y * width + x`.

**Bitmap.fill(value)**: bulk clear-to-black uses `bytes(len(...))` instead of a list comprehension to avoid allocating a temporary list.

### 3. Effects (patterns/*.py)

Each effect subclass of `Effect` (rgb_display.py) maintains its own `bitmap` and `palette`, updated each tick:

| Effect | Description |
|--------|-------------|
| `Fireworks` | Particles with gravity, random burst colors from a pre-shuffled palette |
| `Flame` | Cell automaton: each cell averages neighbors and drifts upward |
| `NightSky` | Twinkling stars with occasional meteor streaks |
| `Honeycomb` | Hexagonal tiling with shifting neighbor-averaged colors |
| `PngDisplay` | Static or animated PNG rendered from flash |
| `VideoDisplay` | Frame sequence from flash, same blitting approach |

All effects implement `tick()` (update animation state) and use the inherited `render(canvas)` which blits nonzero palette indices to the canvas, with optional `scale > 1` for pixel-doubling on larger panels.

### 4. Scroller (scroller.py)

Draws scrolling text using the hzeller library's `graphics.DrawText` with a BDF font. Two text copies scroll right-to-left, centered in each 64×32 panel, with the lower one lagging by `offset_seconds`. Brightness is applied via `graphics.Color` dimming before drawing.

### 5. EffectCoordinator (main.py)

Manages the idle cycle and message transitions:

- **Idle**: cycles through effects `[video, png, honeycomb, flame, fireworks, nightsky]` on each new message
- **Message arrival**: fades out current effect (`fade_seconds=4`), switches effect, fades in text
- **Gamma correction**: `b = linear ** gamma` where gamma=2.2 applies perceptual brightness (human vision is nonlinear)

```
fade out:  brightness 1.0 → 0.0  (current effect)
fade in:   brightness 0.0 → 1.0  (new effect + text)
```

Fade is throttled: palette writes are paced to `fade_step=0.04s` so the main loop doesn't rewrite the palette faster than the panel refreshes.

### 6. MessageManager (lib_shared/message_manager.py)

Receives MQTT envelopes (`type="message"` or `type="config"`). On a new message, calls `coordinator.request_message(body)` which triggers the effect fade and text display.

## Key Classes

| Class | File | Role |
|--------|------|------|
| `Display` | rgb_display.py | RGBMatrix setup, double-buffer, `render()` |
| `Bitmap` | rgb_display.py | Flat palette-index buffer, `bitmap[x, y]` access |
| `Palette` | rgb_display.py | Index → 0xRRGGBB color mapping |
| `Effect` | rgb_display.py | Base class: brightness fade + blit to canvas |
| `Scroller` | scroller.py | BDF font text rendering, scrolling, brightness |
| `EffectCoordinator` | main.py | Idle cycling, fade transitions, main loop |
| `Fireworks` | patterns/fireworks.py | Particle burst animation |
| `Flame` | patterns/flame.py | Cellular automaton fire |
| `NightSky` | patterns/nightsky.py | Star field with meteors |

## Display Geometry

```
64 cols × 64 rows logical (chain=2, U-mapper folds two 64×32 panels)

Row 0  ──────────────────────────────  ← upper panel (center row = 16)
Row 31 ──────────────────────────────
Row 32 ──────────────────────────────  ← lower panel (center row = 48)
Row 63 ──────────────────────────────

Text scrolls in both panels simultaneously:
  Upper: centered at row 16 baseline
  Lower: centered at row 48 baseline, offset by offset_seconds
```

## Main Loop

```python
while True:
    coordinator.tick()  # handles fade state machine + effect/text updates
    display.render(coordinator.effects[coordinator.idx], coordinator.scroller)
```

No `time.sleep` — `SwapOnVSync` blocks until the next panel refresh (~60–144 Hz depending on hardware configuration), pacing the loop automatically.
