#!/usr/bin/env bash
# Launch the heart-matrix-controller (64x64 LED panel display) on the Raspberry Pi.
# Invoked by lindsay_50.service. Runs as root (the rgbmatrix library needs GPIO).
#
# Issue #49: this script now exec's loader.py — the systemd entrypoint that
# handles the blue/green upgrade flow (see heart-matrix-controller/loader.py).
# loader.py then os.execvpe's main.py once the right version is staged and
# the status.json probe confirms it's healthy. systemd sees main.py as the
# direct child so signal handling is preserved.
set -e

# Where this repo is cloned on the Pi — adjust if yours differs.
REPO_DIR="${REPO_DIR:-/srv/lindsay-50}"

# Run from the worktree's heart-matrix-controller/ dir so config_reader
# (called by both loader.py and main.py) finds settings.toml in cwd.
# This mirrors the v1 startup script, which did `cd heart-matrix-controller`.
# The `current` symlink may point at any v-<sha>/ worktree after an upgrade.
cd "$REPO_DIR/current/heart-matrix-controller"

# lib_shared lives at the worktree root (current/ symlink target). Setting
# PYTHONPATH to $REPO_DIR/current — not $REPO_DIR — is critical: on the
# bare-repo+worktree layout, $REPO_DIR itself is the bare .git/ with no
# working tree, so $REPO_DIR/lib_shared/ does not exist and `import
# lib_shared` would fail. Even on a non-bare clone where $REPO_DIR/lib_shared
# DOES exist, that copy is whatever the main branch's working tree contains
# — typically an older commit than the worktree just staged. Loading the
# stale lib_shared/ silently runs the wrong code: debug prints in tick()
# never fire, state-machine transitions don't apply, etc. PYTHONPATH must
# point at the worktree so the loader and the exec'd main.py load the SAME
# version of lib_shared/effects_coordinator.py the worktree was staged at.
# LOG_LEVEL is read by both loader.py and main.py.
export PYTHONPATH="$REPO_DIR/current"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# System Python with rgbmatrix installed via setup-pi.sh. (No venv on this
# single-purpose Pi — keeps the install trivial.)
exec python3 "$REPO_DIR/current/heart-matrix-controller/loader.py"