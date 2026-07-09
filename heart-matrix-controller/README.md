# Heart Matrix Controller

Runs on a Raspberry Pi with a 64×64 HUB75 LED panel (two stacked 64×32 panels, serpentine-wired). Receives messages via MQTT from the Flask server and renders scrolling text over animated background effects.

## Hardware

- **Panel**: 64×64 RGB HUB75 LED matrix (two 64×32 panels chained)
- **Controller**: Raspberry Pi with [hzeller rpi-rgb-led-matrix](https://github.com/hzeller/rpi-rgb-led-matrix) library
- **Configuration**: `settings.toml` or environment variables

## Local development

Running this code on a laptop (e.g. from PyCharm, VS Code, or a plain
terminal) is supported and is the fastest way to iterate on patterns,
effects, and the message pipeline. The Pi deployment section below is
a production overlay on top of the same code; it adds an upgrade
manager and a systemd unit, but the render loop is `main.py` either
way.

### Two entrypoints, two different jobs

| Entrypoint | When to use | What it does |
|---|---|---|
| `python3 main.py` | **Laptop dev**, or any manual run | Runs the render loop + MQTT subscriber directly. No upgrade management. Reads `settings.toml` for MQTT/panel/FONT_PATH and that's it. |
| `python3 loader.py` (or systemd) | **Pi production** | Manages a `current/` symlink pointing at a per-SHA `v-<sha>/` worktree, decides whether to upgrade based on `AUTO_UPDATE`, then `os.execvpe`s into `current/heart-matrix-controller/main.py`. The loader is *not* a dev entrypoint — running it on a laptop won't work unless you've staged a `current/` symlink and have the conventional `/srv/lindsay-50` repo path. |

The `/current/heart-matrix-controller/main.py` path you sometimes see in
stack traces is the loader-managed symlink. It only resolves under the
production layout; if you see `FileNotFoundError: .../current/...` on
your laptop, you're accidentally pointing a debugger or script at the
loader instead of `main.py` — switch the script path in your IDE and the
error goes away.

### Running `main.py` directly

From the repo root, with the venv activated and the working directory
set to `heart-matrix-controller/` (so `settings.toml` and the relative
`FONT_PATH` resolve):

```bash
source .venv/bin/activate
cd heart-matrix-controller
sudo PYTHONPATH=.. LOG_LEVEL=INFO python3 main.py
```

`sudo` is required because the hzeller library drives GPIO directly.
`PYTHONPATH=..` puts the repo root on `sys.path` so `lib_shared.*`
imports resolve. `LOG_LEVEL=DEBUG` is useful the first time you boot
locally — it surfaces the `effects_loader: loaded N effects from
<path>` line so you can see which config won the precedence fight.

What this **does not** do:

- No version upgrade. The loader is not in the process tree.
- No MQTT-driven `check-for-update` swap. The handler is wired but
  gates on `AUTO_UPDATE` (see below).
- No systemd supervision. Stop with Ctrl-C; SIGTERM via `kill` is also
  caught (the SIGTERM handler turns it into `SystemExit`).

### `AUTO_UPDATE` — what it actually gates

`AUTO_UPDATE` is **only** read by `loader.py` (boot-time upgrade
decision) and `check_for_update.py` (runtime MQTT envelope handler).
`main.py` itself does not import it. Concretely:

| Run mode | `AUTO_UPDATE=true` | `AUTO_UPDATE=false` (default) |
|---|---|---|
| `python3 main.py` (local) | Runtime `check-for-update` MQTT envelopes will try to `os.execvpe` into `/srv/lindsay-50/.../loader.py`, which **doesn't exist on a laptop** — your process will die the first time Flask publishes one. Set `AUTO_UPDATE=false` for local dev. | MQTT `check-for-update` envelopes are silently logged and dropped. Safe default. |
| `python3 loader.py` (production) | Loader runs the upgrade flow on boot and on MQTT hints. Production behavior. | Loader skips the upgrade flow and execs the existing `current/` worktree. Pinned-version posture. |

Settings precedence (per `config_reader.py`): env var > `settings.toml`.
So for a one-off local-dev run you can leave `settings.toml` alone and
just prefix the command:

```bash
sudo PYTHONPATH=.. AUTO_UPDATE=false LOG_LEVEL=INFO python3 main.py
```

### Effects settings — the override mechanism

The canonical effect list lives at
`lib_shared/config/effects_settings.json` (in git, schema_version=1,
9 entries — 7 enabled, 2 asset-dependent patterns disabled by default).
**Editing that file directly is the wrong move for local dev**: any
local change you commit gets stomped on the next `git pull`, and any
uncommitted change gets stomped the next time `EffectsSettings()`
rebuilds from the loader.

The override chain (highest precedence first):

1. `EFFECTS_SETTINGS_OVERRIDE` env var pointing at a JSON file path.
   Useful for tests and advanced operators.
2. `config_overrides/effects_settings.json` at the repo root. The
   directory is **gitignored** (`config_overrides/*` with
   `!config_overrides/.gitkeep`); the file inside is yours to keep.
3. The canonical file (read-only from your perspective as a local dev).

Override semantics are **REPLACE**, not merge — your override file
must contain every field the canonical carries
(`schema_version`, `effects[]`, `fade_seconds`, `hold_seconds`,
`intro_seconds`, `idle_seconds`, `recent_count`). Missing fields fall
back to the constructor's defaults, not to the canonical file's
values, so be explicit.

A minimal override that swaps one effect for your own and shortens the
idle cycle:

```json
{
  "schema_version": 1,
  "effects": [
    {"name": "Hyperspace", "module": "lib_shared.patterns.hyperspace", "class_name": "Hyperspace", "enabled": true},
    {"name": "Fireworks", "module": "lib_shared.patterns.fireworks", "class_name": "Fireworks", "enabled": true},
    {"name": "MyPattern", "module": "lib_shared.patterns.my_pattern", "class_name": "MyPattern", "enabled": true}
  ],
  "fade_seconds": 1.0,
  "hold_seconds": 7.0,
  "intro_seconds": 3.0,
  "idle_seconds": 30.0,
  "recent_count": 5
}
```

Drop that at `config_overrides/effects_settings.json` and the next
`main.py` boot will load it. The boot log will print:

```
effects_loader: loaded 3 effects from <repo>/config_overrides/effects_settings.json (schema_version=1)
```

…which is your signal that the override won. If you see the canonical
path instead, the loader fell through to the default — check that the
override file is valid JSON and matches the schema.

**Custom pattern modules:** drop a `MyPattern(Effect)` subclass into
`lib_shared/patterns/my_pattern.py`, reference its module + class name
from the override file as above, and the loader resolves it via
dynamic import (`importlib.import_module(...)` + `getattr(...)`). The
override file is the only thing that needs to know your pattern
exists — `main.py` and the rest of the pipeline pick it up
transparently.

**Precedence edge case:** if both `EFFECTS_SETTINGS_OVERRIDE` and the
gitignored override file exist, the env var wins. If the env var
points at a missing file, the loader logs a warning and falls back to
the gitignored override (then the canonical).

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
5. Pipes the local `settings.toml` to the Pi via `ssh ... cat > FILE`
   (atomic `.tmp` + `mv`). `sftp` and `scp` were tried first but both
   ignore `SSH_ASKPASS_REQUIRE=force` on macOS OpenSSH, breaking the
   password path — the pipe-over-ssh path is the one that works.
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

### SSH access — publickey and password

The Pi accepts root login via both **publickey** (the unattended
default; what `provision-pi.sh` prefers) and **password** (used
for ad-hoc / shared access, and as a `provision-pi.sh` fallback
when run from a TTY — the script prompts once and routes the
remaining ssh calls through an encrypted SSH_ASKPASS, with
the plaintext password never touching disk). Enable both once with:

```bash
# On the Pi, as a user with sudo (e.g. the default `rosie` user):
sudo cp /etc/ssh/sshd_config /etc/ssh/sshd_config.bak-$(date +%F)   # optional safety
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
sudo systemctl restart ssh
sudo sshd -T | grep -iE "permitroot|passwordauth"
# expect: permitrootlogin yes / passwordauthentication yes
```

Setting `PermitRootLogin yes` (rather than `prohibit-password`)
allows both methods simultaneously — that's the point: pick whichever
works at the moment. `setup-pi.sh` doesn't touch sshd_config, so
this state persists across all subsequent re-runs and version bumps.

#### Publickey path (recommended — what `provision-pi.sh` uses)

Key-only auth, no prompts. Required for `provision-pi.sh`'s
`BatchMode=yes` preflight.

On the laptop, generate or pick a key:

```bash
test -f ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
# (If you already use ssh keys for GitHub / Heroku, you have one already.)
```

Install that key into the Pi's root `authorized_keys`. The cleanest
path avoids password auth entirely:

```bash
# 1. Print the key on the laptop:
cat ~/.ssh/id_ed25519.pub
# 2. On the Pi, paste the line:
sudo install -d -m 700 -o root -g root /root/.ssh
echo 'PASTE_PUBKEY_LINE_HERE' | sudo tee /root/.ssh/authorized_keys >/dev/null
sudo chmod 600 /root/.ssh/authorized_keys
```

Or push it over the working `pi`/`rosie` user's SSH:

```bash
scp ~/.ssh/id_ed25519.pub <user>@lindsay-50.local:/tmp/id_ed25519.pub
ssh <user>@lindsay-50.local \
  'sudo install -m 600 -o root -g root /tmp/id_ed25519.pub /root/.ssh/authorized_keys \
   && sudo rm /tmp/id_ed25519.pub'
```

Verify from the laptop — should print `ok` with **no password prompt**:

```bash
ssh root@lindsay-50.local 'echo ok'
```

#### Password path (fallback for ad-hoc / shared access)

With `PasswordAuthentication yes` set above, anyone with the root
password can `ssh root@lindsay-50.local` from a fresh machine:

```bash
# Set the root password first (you'll be prompted twice):
ssh root@lindsay-50.local 'sudo passwd root'

# Then from any machine with this Pi's network reach:
ssh root@lindsay-50.local   # password prompt
```

To turn password off later (key-only mode) without losing the
publickey install:

```bash
sudo sed -i 's/^PasswordAuthentication yes$/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo systemctl restart ssh
```

`PermitRootLogin yes` should remain `yes` even in key-only mode (so
publickey auth still works) — only flip it to `prohibit-password`
if you want the server to actively reject every other method.

#### Troubleshooting — `passwordauthentication no` after enabling

If `sudo sshd -T | grep passwordauth` still reports `no` after the
sed + restart, a drop-in in `/etc/ssh/sshd_config.d/` is overriding
the main file. Drop-ins load alphabetically *before* the main-file
directives and win via first-match-wins — so the canonical Debian /
Ubuntu gotcha is:

```
/etc/ssh/sshd_config.d/50-cloud-init.conf   # says PasswordAuthentication no
```

This is Ubuntu's cloud-init setting, present on Debian-family
images even when cloud-init isn't actively used. It silently
overrides your main-file edit. On a Pi that doesn't run cloud-init,
the file is dead config and safe to remove:

```bash
sudo rm /etc/ssh/sshd_config.d/50-cloud-init.conf
sudo systemctl restart ssh
sudo sshd -T | grep -iE "permitroot|passwordauth"
# expect: permitrootlogin yes / passwordauthentication yes
```

Alternatively, if you want to keep the drop-in (e.g. cloud-init is
in use elsewhere), make yours load earlier with a `00-` prefix —
alphabetically before any `50-`-ranged files:

```bash
sudo mv /etc/ssh/sshd_config.d/enable-password-for-bootstrap.conf \
        /etc/ssh/sshd_config.d/00-enable-password.conf
sudo systemctl restart ssh
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
