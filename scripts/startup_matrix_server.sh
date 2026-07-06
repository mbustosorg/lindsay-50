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

# lib_shared lives at the repo root; LOG_LEVEL is read by both loader.py and main.py.
export PYTHONPATH="$REPO_DIR"
export LOG_LEVEL="${LOG_LEVEL:-INFO}"

# System Python with rgbmatrix installed via setup-pi.sh. (No venv on this
# single-purpose Pi — keeps the install trivial.)
exec python3 "$REPO_DIR/current/heart-matrix-controller/loader.py"